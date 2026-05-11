# ═══════════════════════════════════════════════════════════════
#  AEROTAX BACKEND — app.py
#  Deploy auf Render
#
#  Umgebungsvariablen (in Render Dashboard setzen):
#    ANTHROPIC_API_KEY      = sk-ant-...
#    STRIPE_SECRET_KEY      = sk_live_...
#    STRIPE_WEBHOOK_SECRET  = whsec_...
#    AEROTAX_PRICE_ID       = price_... (15 EUR Produkt in Stripe)
#    FRONTEND_URL           = https://aerosteuer.de
#    PORT                   = 5000
# ═══════════════════════════════════════════════════════════════

import os, io, uuid, json, re, tempfile, gc
import hashlib as _hashlib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import stripe
import anthropic
import pdfplumber
import base64
try:
    from PIL import Image
    PIL_AVAILABLE = True
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
        HEIF_AVAILABLE = True
    except ImportError:
        HEIF_AVAILABLE = False
except ImportError:
    PIL_AVAILABLE = False
    HEIF_AVAILABLE = False

# ── SUPABASE CLIENT (für persistente QA + Sessions) ──
try:
    from supabase import create_client as _create_sb_client
    SUPABASE_URL = os.environ.get('SUPABASE_URL', '')
    SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_KEY', '')
    if SUPABASE_URL and SUPABASE_KEY:
        sb = _create_sb_client(SUPABASE_URL, SUPABASE_KEY)
        SB_AVAILABLE = True
        print(f"[supabase] connected to {SUPABASE_URL}")
    else:
        sb = None
        SB_AVAILABLE = False
        print("[supabase] not configured (env vars missing) — fallback to file-based")
except Exception as e:
    sb = None
    SB_AVAILABLE = False
    print(f"[supabase] init failed: {e} — fallback to file-based")
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, PageBreak, HRFlowable, LongTable,
                                 Image as RLImage)
from reportlab.lib.enums import TA_RIGHT, TA_CENTER, TA_LEFT

# ── APP SETUP ─────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins=[
    os.getenv('FRONTEND_URL', 'https://aerosteuer.de'),
    'https://aerosteuer.de',
    'https://aerosteuer.pages.dev',
    re.compile(r'^https://[a-z0-9-]+\.aerosteuer\.pages\.dev$'),  # Cloudflare Preview-Deploys
    'http://localhost:3000',
    'http://localhost:8080',
])

stripe.api_key        = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET        = os.getenv('STRIPE_WEBHOOK_SECRET')
ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY')
PRICE_ID              = os.getenv('AEROTAX_PRICE_ID')
FRONTEND_URL          = os.getenv('FRONTEND_URL','https://aerosteuer.de')

# ── v8: Reader-/Engine-Versionierung ──
APP_VERSION = '8.3'
APP_BUILD   = 'strict-fahrtage-arbeitstage-pdf-cleanup-2026-05-10'
READER_VERSIONS = {
    'lsb': 'sonnet_lsb_v8_0',
    'se':  'sonnet_se_structured_v8_0',
    'dp':  'sonnet_dp_structured_v8_0',
}
ENGINE_VERSION = 'deterministic_v8_0'
PROMPT_VERSION = 'v8_0'

# In-memory store (in Produktion: Redis oder S3)
_store = {}

# ── BMF AUSLANDSPAUSCHALEN ────────────────────────────────────────
# Quelle: bundesfinanzministerium.de offizielle BMF-Schreiben 2023-2026
# Auto-generated via /tmp/parse_bmf_v5.py (Spalten: 24h, An-/Abreise+8h)
from bmf_data import BMF_AUSLAND_BY_YEAR, IATA_TO_BMF


def bmf_lookup(iata, year):
    """Resolves IATA → BMF (24h, an_abreise) für ein Jahr.
    Fallback-Kette: City → 'im Übrigen' → Country → None.
    """
    bmf = BMF_AUSLAND_BY_YEAR.get(year) or BMF_AUSLAND_BY_YEAR.get(2025)
    if not bmf: return None
    key = IATA_TO_BMF.get(iata)
    if not key: return None
    if key in bmf: return bmf[key]
    if ' – ' in key:
        parent = key.split(' – ')[0]
        alt = f"{parent} – im Übrigen"
        if alt in bmf: return bmf[alt]
        if parent in bmf: return bmf[parent]
    return None


# Backward-Compat-Alias für alten Code
BMF_2025 = {iata: bmf_lookup(iata, 2025) for iata in IATA_TO_BMF if bmf_lookup(iata, 2025)}

# ══════════════════════════════════════════════════════════════════
#  STRIPE ROUTEN
# ══════════════════════════════════════════════════════════════════

@app.route('/api/create-checkout', methods=['POST'])
def create_checkout():
    data = request.get_json() or {}
    ref  = str(uuid.uuid4())

    # Formulardaten temporär speichern
    _store[ref] = {
        'form': data,
        'files': {},
        'paid': False,
        'expires': datetime.utcnow() + timedelta(hours=2),
    }

    if not stripe.api_key or not PRICE_ID:
        return jsonify({'error': 'Stripe ist nicht korrekt konfiguriert.'}), 500

    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{'price': PRICE_ID, 'quantity': 1}],
        mode='payment',
        success_url=f'{FRONTEND_URL}/?paid=1&ref={ref}#tool',
        cancel_url=f'{FRONTEND_URL}/?paid=0#tool',
        metadata={'ref': ref},
        locale='de',
        invoice_creation={'enabled': True},
    )
    return jsonify({'checkout_url': session.url, 'ref': ref})


@app.route('/api/payment-status/<ref>', methods=['GET'])
def payment_status(ref):
    """Frontend prüft nach Stripe-Redirect ob die Zahlung wirklich durch ist."""
    entry = _store.get(ref)
    if not entry:
        return jsonify({'paid': False, 'error': 'ref not found'}), 404
    return jsonify({'paid': bool(entry.get('paid')), 'ref': ref})


@app.route('/api/create-payment-intent', methods=['POST'])
def create_payment_intent():
    """Creates a Stripe PaymentIntent for Stripe Elements (no redirect).
    Reused ein Pre-Upload-ref wenn vom Frontend mitgegeben (so überleben Files den Reload)."""
    try:
        data = request.get_json() or {}
        amount = int(data.get('amount', 1999))
        currency = data.get('currency', 'eur')
        existing_ref = (data.get('ref') or '').strip()
        # Reuse pre-upload ref wenn vorhanden + im Store
        if existing_ref and existing_ref in _store:
            ref = existing_ref
            # Form-Daten + paid-Status nicht überschreiben — nur erweitern
            _store[ref]['kind'] = 'payment'
            _store[ref]['expires'] = datetime.utcnow() + timedelta(hours=26)
        else:
            ref = str(uuid.uuid4())
            _store[ref] = {
                'form':    data,
                'files':   {},
                'paid':    False,
                'expires': datetime.utcnow() + timedelta(hours=2),
            }

        if not stripe.api_key:
            return jsonify({'error': 'Stripe ist nicht korrekt konfiguriert.'}), 500

        intent = stripe.PaymentIntent.create(
            amount=amount,
            currency=currency,
            automatic_payment_methods={'enabled': True},
            metadata={'ref': ref},
        )
        return jsonify({
            'client_secret': intent.client_secret,
            'ref': ref
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# v11: CAS (Dienstplan/Roster) ist ab v11 das 3. Pflicht-Dokument
# anstelle der Flugstundenübersicht (dp). dp bleibt vorerst im File-Keys-Tupel
# damit Legacy-Code in altem Hybrid-Pfad nicht crasht — wird in Phase 3-5
# komplett entfernt zusammen mit dem DP-Reader.
_ALL_FILE_KEYS = (
    'lsb', 'se', 'cas',  # v11 Pflicht
    'dp',                 # Legacy v10 — wird in Phase 3-5 entfernt
    'einsatz',           # Legacy (alter Einsatzplan-Slot, ungenutzt)
    'stb', 'gew', 'arb', 'fort', 'tel', 'konz',
    'lapt', 'fach', 'reini', 'bewer',
    'bu', 'haft', 'kv', 'rv', 'leb', 'haus',
    'arzt', 'zahn', 'medi', 'pfle', 'under', 'kata',
    'spen', 'part', 'kind', 'hand', 'haed',
)

# v11 Pipeline-Version-Flag — bleibt 'v10_legacy' während Phase 2-5 Migration,
# wird in Phase 6 final auf 'v11_cas_primary' geflippt sobald CAS-Reader fertig.
AEROTAX_PIPELINE_VERSION = os.environ.get('AEROTAX_PIPELINE_VERSION', 'v10_legacy')

UPLOAD_TTL_HOURS = 4   # Pre-Upload nur kurz aufbewahren — nach Auswertung gelöscht


def _save_uploaded_files_supabase(ref, files_dict, hours=UPLOAD_TTL_HOURS):
    """Persist uploaded files to Supabase 'uploaded_files' table.
    files_dict: { key: [(bytes, filename), ...] } oder { key: [bytes, ...] }
    """
    if not SB_AVAILABLE or not files_dict:
        return False
    expires = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
    rows = []
    for key, items in files_dict.items():
        for idx, item in enumerate(items):
            data = item[0] if isinstance(item, tuple) else item
            fname = item[1] if isinstance(item, tuple) and len(item) > 1 else f'{key}_{idx}'
            try:
                rows.append({
                    'ref':       ref,
                    'key':       key,
                    'idx':       idx,
                    'filename':  fname or f'{key}_{idx}',
                    'data_b64':  base64.b64encode(data).decode(),
                    'expires_at': expires,
                })
            except Exception as e:
                print(f"[supabase upload] encode fail {key}/{idx}: {e}")
    if not rows:
        return False
    try:
        # Erst alte Einträge für ref löschen, dann neu inserten
        sb.table('uploaded_files').delete().eq('ref', ref).execute()
        # In Batches von 5 inserten — JSONB-Rows können groß sein
        for i in range(0, len(rows), 5):
            sb.table('uploaded_files').insert(rows[i:i+5]).execute()
        print(f"[supabase upload] ref={ref[:8]}: {len(rows)} Dateien persistiert")
        return True
    except Exception as e:
        print(f"[supabase upload] save fail: {e}")
        return False


def _load_uploaded_files_supabase(ref):
    """Return {key: [(bytes, filename), ...]} or {} if nothing/expired."""
    if not SB_AVAILABLE or not ref:
        return {}
    try:
        r = sb.table('uploaded_files').select('*').eq('ref', ref).execute()
        if not r.data:
            return {}
        out = {}
        now = datetime.utcnow()
        for row in r.data:
            try:
                exp_str = (row.get('expires_at') or '').replace('Z', '').split('+')[0]
                if datetime.fromisoformat(exp_str) < now:
                    continue
            except: pass
            key = row['key']
            data = base64.b64decode(row['data_b64'])
            fname = row.get('filename') or f"{key}_{row.get('idx',0)}"
            out.setdefault(key, []).append((row.get('idx', 0), data, fname))
        # Sortieren nach idx, dann strip
        return {k: [(d, f) for (_, d, f) in sorted(v)] for k, v in out.items()}
    except Exception as e:
        print(f"[supabase upload] load fail: {e}")
        return {}


def _delete_uploaded_files_supabase(ref):
    """Cleanup nach erfolgreicher Auswertung — Datenschutz first."""
    if not SB_AVAILABLE or not ref:
        return
    try:
        sb.table('uploaded_files').delete().eq('ref', ref).execute()
    except Exception as e:
        print(f"[supabase upload] delete fail: {e}")


@app.route('/api/init-upload-session', methods=['POST'])
def init_upload_session():
    """Erzeugt einen ref-Slot OHNE Stripe — Frontend kann sofort beim Upload Files persistieren.
    Beim späteren /api/create-payment-intent kann derselbe ref reused werden."""
    ref = str(uuid.uuid4())
    _store[ref] = {
        'form':     {},
        'files':    {},
        'paid':     False,
        'expires':  datetime.utcnow() + timedelta(hours=26),  # 24h + Puffer
        'kind':     'preupload',
    }
    return jsonify({'ref': ref})


@app.route('/api/upload-files', methods=['POST'])
def upload_files():
    """Pre-Upload: Frontend lädt Dateien VOR der Bezahlung hoch, damit sie bei
    3DS-Redirect oder Reload nicht verloren gehen. Files in _store[ref]['files'].
    """
    ref = request.form.get('ref', '').strip()
    if not ref or ref not in _store:
        return jsonify({'error': 'ref not found'}), 404

    saved_count = 0
    for key in _ALL_FILE_KEYS:
        files = request.files.getlist(key)
        if files:
            normalized = []
            for f in files:
                try:
                    # _normalize_upload liefert bereits (bytes, filename) als Tupel
                    normalized.append(_normalize_upload(f.read(), f.filename))
                    saved_count += 1
                except Exception as e:
                    print(f"[upload-files] {key}/{f.filename} failed: {e}")
            if normalized:
                _store[ref]['files'][key] = normalized

    # Parallel auf Supabase persistieren — überlebt Render-Restart
    if _store[ref].get('files'):
        _save_uploaded_files_supabase(ref, _store[ref]['files'])

    print(f"[upload-files] ref={ref[:8]} {saved_count} Dateien gespeichert")
    return jsonify({'status': 'ok', 'count': saved_count})


_processed_stripe_events = {}  # event_id → timestamp; Idempotenz für Webhooks
_consumed_payment_intents = {}  # pi_id → consumed_at; verhindert Replay
_ip_rate_buckets = {}  # ip → list[ts]; rolling window 1h für /api/process Anti-Abuse


def _ip_rate_limited(ip, endpoint='process', limit=20, window_sec=3600):
    """Sliding-Window Rate-Limit pro IP. Liefert True wenn limit überschritten.
    Default: 20 Versuche pro Stunde pro IP für /api/process. Stripe-Payment ist
    der primäre Anti-Abuse-Layer (kostet Geld); IP-Limit fängt nur Brute-Force
    auf das Endpoint selbst (z.B. Promo-Code-Raten) ab."""
    if not ip: return False
    now = datetime.utcnow().timestamp()
    cutoff = now - window_sec
    key = f'{ip}:{endpoint}'
    bucket = _ip_rate_buckets.setdefault(key, [])
    # Alte Entries räumen + Cleanup wenn dict zu groß (>10000 keys)
    bucket[:] = [t for t in bucket if t > cutoff]
    if len(_ip_rate_buckets) > 10000:
        for k in list(_ip_rate_buckets.keys())[:5000]:
            _ip_rate_buckets.pop(k, None)
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


def _client_ip():
    """Extrahiert Client-IP, respektiert X-Forwarded-For (Cloudflare/Render-Proxies)."""
    xff = request.headers.get('X-Forwarded-For', '')
    if xff:
        return xff.split(',')[0].strip()
    return request.remote_addr or ''

@app.route('/api/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """Markiert _store[ref].paid=True. Auswertung selbst läuft NICHT hier — der
    Frontend-Flow ruft /api/process direkt auf nach Payment-Element Erfolg.
    Webhook bleibt als Backup / Confirmation bestehen."""
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')

    if not WEBHOOK_SECRET:
        return jsonify({'error': 'Stripe Webhook Secret fehlt.'}), 500

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    # Idempotenz: gleicher Webhook-Event nur 1x verarbeiten
    eid = event.get('id', '')
    if eid and eid in _processed_stripe_events:
        return jsonify({'status': 'ok', 'idempotent': True})
    if eid:
        # Alte Einträge >1h aufräumen
        cutoff = datetime.utcnow() - timedelta(hours=1)
        for k, ts in list(_processed_stripe_events.items()):
            if ts < cutoff:
                _processed_stripe_events.pop(k, None)
        _processed_stripe_events[eid] = datetime.utcnow()

    if event['type'] in ('checkout.session.completed', 'payment_intent.succeeded'):
        obj = event['data']['object']
        ref = (obj.get('metadata') or {}).get('ref', '')
        if ref and ref in _store:
            _store[ref]['paid'] = True
            print(f"[stripe-webhook] ref {ref[:8]} marked paid via {event['type']}")

    return jsonify({'status': 'ok'})


@app.route('/api/status/<ref>', methods=['GET'])
def check_status(ref):
    entry = _store.get(ref)
    if not entry:
        return jsonify({'status': 'not_found'}), 404
    if entry.get('dl_token'):
        return jsonify({'status': 'ready',
                        'download_url': f'/api/download/{entry["dl_token"]}'})
    elif entry.get('paid'):
        return jsonify({'status': 'processing'})
    else:
        return jsonify({'status': 'pending'})


PDF_TTL_HOURS = 24

def _save_pdf(token, pdf_bytes, filename, hours=PDF_TTL_HOURS):
    """Persist PDF in Supabase + In-Memory _store. Beide Wege werden versucht."""
    expires = datetime.utcnow() + timedelta(hours=hours)
    _store[token] = {
        'pdf_bytes': pdf_bytes,
        'filename':  filename,
        'expires':   expires,
    }
    if SB_AVAILABLE:
        try:
            sb.table('pdfs').upsert({
                'token':      token,
                'filename':   filename,
                'pdf_b64':    base64.b64encode(pdf_bytes).decode(),
                'expires_at': expires.isoformat(),
            }).execute()
        except Exception as e:
            print(f"[supabase] pdf save fail: {e}")


def _load_pdf(token):
    """In-Memory zuerst, dann Supabase. Returns (bytes, filename, expires) or None."""
    entry = _store.get(token)
    if entry and entry.get('pdf_bytes'):
        return entry['pdf_bytes'], entry.get('filename') or 'AeroTax_Auswertung.pdf', entry.get('expires')
    if SB_AVAILABLE:
        try:
            r = sb.table('pdfs').select('*').eq('token', token).limit(1).execute()
            if r.data:
                row = r.data[0]
                exp = None
                try:
                    exp_str = (row.get('expires_at') or '').replace('Z', '').split('+')[0]
                    exp = datetime.fromisoformat(exp_str)
                    if exp < datetime.utcnow():
                        return None
                except: pass
                # downloaded_at aus Supabase mit übernehmen — sonst Replay-Schutz nach Worker-Restart wirkungslos
                downloaded_at = None
                try:
                    da_str = (row.get('downloaded_at') or '').replace('Z', '').split('+')[0]
                    if da_str:
                        downloaded_at = datetime.fromisoformat(da_str)
                except: pass
                pdf_bytes = base64.b64decode(row['pdf_b64'])
                # In-Memory cachen inklusive downloaded_at
                _store[token] = {
                    'pdf_bytes':     pdf_bytes,
                    'filename':      row.get('filename'),
                    'expires':       exp,
                    'downloaded_at': downloaded_at,
                }
                return pdf_bytes, row.get('filename') or 'AeroTax_Auswertung.pdf', exp
        except Exception as e:
            print(f"[supabase] pdf load fail: {e}")
    return None


@app.route('/api/download/<token>', methods=['GET'])
def download_pdf(token):
    res = _load_pdf(token)
    if not res:
        abort(404)
    pdf_bytes, filename, expires = res
    if expires and datetime.utcnow() > expires:
        abort(410)
    # Replay-Schutz: nach 1. erfolgreichem Download Token markieren
    # Cache-Header: kein Caching durch Browser/CDN damit zweiter Klick wirklich Backend trifft
    entry = _store.get(token) or {}
    if entry.get('downloaded_at'):
        # Innerhalb von 60s den Re-Click erlauben (User klickt manchmal 2x), danach blocken
        try:
            dl_at = entry['downloaded_at']
            if isinstance(dl_at, str):
                dl_at = datetime.fromisoformat(dl_at.replace('Z',''))
            if (datetime.utcnow() - dl_at).total_seconds() > 60:
                print(f"[download] token {token[:8]}: blocked re-download (>60s nach 1. Download)")
                abort(410)
        except: pass
    # PDF-Download = finaler Bescheid → Edit-Token verbrauchen + Download-Marker setzen
    try:
        sess_tok = entry.get('session_token')
        if sess_tok:
            _mark_session_consumed(sess_tok)
        _store.setdefault(token, {})['downloaded_at'] = datetime.utcnow()
        # Auch in Supabase persistieren falls möglich
        if SB_AVAILABLE:
            try:
                sb.table('pdfs').update({'downloaded_at': datetime.utcnow().isoformat()}).eq('token', token).execute()
            except Exception as _se:
                pass  # downloaded_at-Spalte könnte fehlen, in-memory reicht für 60s-Fenster
    except Exception as e:
        print(f"[download] consume-mark fail: {e}")
    response = send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename,
    )
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    return response


def _mark_session_consumed(token):
    """Markiert Session als final (nach PDF-Download). Restore wird damit blockiert."""
    s = _load_session(token)
    if not s: return
    rd = s.get('result_data') or {}
    if rd.get('_consumed_at'): return  # bereits consumed
    rd['_consumed_at'] = datetime.utcnow().isoformat() + 'Z'
    s['result_data'] = rd
    _save_session(token, s)
    print(f"[session] {token[:8]}... consumed (PDF downloaded)")


@app.route('/api/restore-session/<token>', methods=['POST'])
def restore_session(token):
    """User gibt AT-Code ein → bekommt Form-Werte zurück + Free-Retry-Token, kann
    Files erneut hochladen ohne erneut zu zahlen. PDF-Download invalidiert das.
    """
    token = (token or '').strip().upper()
    if not token.startswith('AT-'):
        return jsonify({'error': 'Ungültiges Code-Format'}), 400
    s = _load_session(token)
    if not s:
        return jsonify({'error': 'Code abgelaufen oder ungültig (24h-Frist)'}), 404
    rd = s.get('result_data') or {}
    if rd.get('_consumed_at'):
        return jsonify({
            'error': 'Code bereits verbraucht — du hast die Auswertung als PDF heruntergeladen. '
                     'Eine neue Berechnung erfordert Neukauf.',
            'consumed': True,
        }), 410
    form_inputs = rd.get('_form_inputs') or {}
    if not form_inputs:
        return jsonify({'error': 'Keine speicherbaren Form-Werte gefunden — Session zu alt.'}), 422

    # Free-Retry-Token vergeben (60 Min Fenster für Neuberechnung)
    info = _recovery_tokens.get(token, {})
    _recovery_tokens[token] = {
        'token': token,
        'retries_used': int(info.get('retries_used', 0)),  # nicht hochzählen — Edit ≠ Recovery
        'expires': (datetime.utcnow() + timedelta(minutes=60)).isoformat() + 'Z',
        'kind': 'edit',
    }
    return jsonify({
        'ok': True,
        'form_inputs':      form_inputs,
        'free_retry_token': token,
        'message': 'Du kannst jetzt deine Dateien erneut hochladen und neu berechnen — kostenlos.',
    })


# Demo ohne Zahlung (gibt Fallback-Werte zurück)
@app.route('/api/demo', methods=['POST'])
def demo():
    """Demo — uses provided data or generates random Max Mustermann values."""
    import random
    # Check if client sent fixed data
    req = request.get_json(silent=True) or {}
    
    if req.get('name') == 'Max Mustermann' and req.get('km'):
        # Use the exact data sent by frontend
        r_data = req
        km = float(r_data.get('km', 22))
        fahr_tage = int(r_data.get('fahr_tage', 62))
        arbeitstage = int(r_data.get('arbeitstage', 140))
        hotel_naechte = int(r_data.get('hotel_naechte', 72))
        vma_72 = float(r_data.get('vma_72', 84))
        vma_73 = float(r_data.get('vma_73', 196))
        vma_74 = float(r_data.get('vma_74', 56))
        vma_in = float(r_data.get('vma_in', 336))
        vma_aus = float(r_data.get('vma_aus', 5180))
        fahr = float(r_data.get('fahr', 598.40))
        reinig = float(r_data.get('reinig', 224.00))
        trink = float(r_data.get('trink', 259.20))
        gesamt = float(r_data.get('gesamt', 6597.60))
        ag_z17 = float(r_data.get('ag_z17', 280))
        spesen_g = float(r_data.get('spesen_gesamt', 5920))
        spesen_s = float(r_data.get('spesen_steuer', 1340))
        z77 = float(r_data.get('z77', 4580))
        netto = float(r_data.get('netto', 1737.60))
        name = 'Max Mustermann'
    else:
        # Generate random values
        r = lambda a, b: round(random.uniform(a, b), 2)
        ri = lambda a, b: random.randint(a, b)
        km = ri(15, 60); fahr_tage = ri(45, 70); arbeitstage = ri(110, 150)
        hotel_naechte = ri(50, 80)
        vma_72 = ri(3, 8) * 14; vma_73 = ri(8, 15) * 14; vma_74 = ri(0, 2) * 28
        vma_in = vma_72 + vma_73 + vma_74; vma_aus = r(3500, 6000)
        fahr = round(min(km,20)*fahr_tage*0.30 + max(0,km-20)*fahr_tage*0.38, 2)
        reinig = round(arbeitstage * 1.60, 2); trink = round(hotel_naechte * 3.60, 2)
        gesamt = round(fahr + reinig + trink + vma_in + vma_aus, 2)
        ag_z17 = r(200, 450); spesen_g = r(4000, 7000); spesen_s = r(800, 2000)
        z77 = round(spesen_g - spesen_s, 2); netto = round(gesamt - ag_z17 - z77, 2)
        name = 'Max Mustermann'
    r = lambda a,b: round((a+b)/2, 2)  # safe fallback
    result = {
        'name': name,
        'year': 2025,
        'datum': datetime.now().strftime('%d.%m.%Y'),
        'km': km, 'fahr_tage': fahr_tage,
        'arbeitstage': arbeitstage, 'hotel_naechte': hotel_naechte,
        'vma_72_tage': vma_72//14, 'vma_73_tage': vma_73//14,
        'vma_74_tage': vma_74//28 if vma_74 else 0,
        'vma_72': vma_72, 'vma_73': vma_73, 'vma_74': vma_74,
        'vma_in': vma_in, 'vma_aus': vma_aus,
        'fahr': fahr, 'reinig': reinig, 'trink': trink,
        'gesamt': gesamt, 'ag_z17': ag_z17,
        'spesen_gesamt': spesen_g, 'spesen_steuer': spesen_s,
        'z77': z77, 'netto': netto,
        'brutto': 54200.00, 'lohnsteuer': 7980.00,
        'arbeitgeber': 'Deutsche Lufthansa AG',
        'uploaded_summary': 'Demo-Modus — keine echten Dokumente',
        '_isDemo': True,
        'not_uploaded': '',
        'abrechnungen': [
            {'erstellt': f'{mon:02d}.2025', 'bezeichnung': f'Monat {mon}',
             'gesamt': round(spesen_g/12, 2),
             'steuerpflichtig': round(spesen_s/12, 2),
             'steuerfrei': round((spesen_g-spesen_s)/12, 2)}
            for mon in range(1, 13)
        ],
    }

    pdf   = erstelle_pdf(result)
    token = str(uuid.uuid4())
    _save_pdf(token, pdf, 'AeroTax_Auswertung_Demo_2025.pdf', hours=1)
    safe = {k: v for k, v in result.items()
            if isinstance(v, (int, float, str))}
    return jsonify({'status':'ready',
                    'download_url': f'/api/download/{token}',
                    'data': safe})


# ── BILD-NORMALISIERUNG (HEIC/WEBP/etc → JPEG) + DOWNSCALE ─────
# Task B (v10.4.2): iPhone-Fotos sind oft 4032×3024 (~30 MB decoded RAM).
# Wir scalen auf max 1500px max-Dim runter — Sonnet braucht die Auflösung
# nicht und der RAM-Druck auf Render Free-Tier sinkt deutlich. PDFs bleiben
# unangetastet. EXIF-Rotation wird angewandt (iPhone speichert oft sideways).
_IMAGE_MAX_DIM = 1500
_IMAGE_JPEG_QUALITY = 88


def _normalize_upload(file_bytes, filename=''):
    """Konvertiert exotische Bildformate zu JPEG + downscaled große Bilder.

    Regeln:
      - PDF: immer unverändert
      - HEIC/HEIF/WEBP/TIFF: → JPEG (mit Downscale wenn nötig)
      - JPEG/PNG:
         · wenn max(width,height) > 1500 → Downscale + JPEG-Re-Save
         · wenn EXIF-Orientation ≠ 1 → orientation-fix + JPEG-Re-Save
         · sonst unverändert (kein Quality-Loss)
      - Returns (bytes, filename)
    """
    if not file_bytes:
        return file_bytes, filename
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''

    # PDF: NIEMALS anfassen
    if file_bytes[:4] == b'%PDF' or ext == 'pdf':
        return file_bytes, filename

    if not PIL_AVAILABLE:
        return file_bytes, filename

    try:
        from PIL import ImageOps as _IOps
        img = Image.open(io.BytesIO(file_bytes))
        original_size = img.size

        # EXIF-Orientation anwenden (iPhone-Photos sind oft EXIF-rotated)
        try:
            exif = img.getexif() if hasattr(img, 'getexif') else None
            had_exif_rotation = bool(exif and exif.get(0x0112, 1) != 1)
        except Exception:
            had_exif_rotation = False
        img = _IOps.exif_transpose(img)

        # Downscale wenn zu groß
        needs_resize = max(img.size) > _IMAGE_MAX_DIM
        if needs_resize:
            img.thumbnail((_IMAGE_MAX_DIM, _IMAGE_MAX_DIM), Image.Resampling.LANCZOS)

        # Format-Konvertierung nötig?
        is_jpeg = file_bytes[:3] == b'\xff\xd8\xff'
        is_png = file_bytes[:8] == b'\x89PNG\r\n\x1a\n'
        needs_convert = not (is_jpeg or is_png)  # HEIC/WEBP/TIFF etc.

        # Wenn weder Resize noch Convert noch EXIF-Rotation: original lassen
        # (vermeidet unnötige JPEG-Re-Compression)
        if not (needs_resize or needs_convert or had_exif_rotation):
            return file_bytes, filename

        buf = io.BytesIO()
        img.convert('RGB').save(buf, format='JPEG',
                                  quality=_IMAGE_JPEG_QUALITY, optimize=True)
        new_bytes = buf.getvalue()

        # Neuer Filename
        if needs_convert:
            new_name = (filename.rsplit('.', 1)[0] + '.jpg') if '.' in filename else (filename or 'image') + '.jpg'
        else:
            new_name = filename or 'image.jpg'

        action = []
        if needs_resize: action.append(f'resize {original_size}→{img.size}')
        if needs_convert: action.append(f'convert {ext or "?"}→jpg')
        if had_exif_rotation: action.append('exif-rotate')
        print(f"[image-normalize] {filename}: {', '.join(action)} | "
              f"{len(file_bytes)//1024}KB → {len(new_bytes)//1024}KB")
        return new_bytes, new_name

    except Exception as e:
        print(f"[image-normalize] fehlgeschlagen für {filename}: {e}")
        return file_bytes, filename


# ── PROCESS MIT ECHTEN PDFs ────────────────────────────────────
# Wird vom Frontend aufgerufen wenn echte Dokumente hochgeladen werden
# Unterstützt: Free Promo Code + Paid Flow (nach Webhook)
# ── ASYNC JOB STORE + PERSISTENZ ───────────────────────────────
# In-Memory Job-Store für schnellen Zugriff, plus Disk-Persistierung für Restart-Resilience.
_jobs = {}
_jobs_lock = __import__('threading').Lock()
_JOBS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'jobs_state')
os.makedirs(_JOBS_DIR, exist_ok=True)

# ── WARTESCHLANGE ──────────────────────────────────────────────
# Verhindert dass mehrere Auswertungen parallel laufen → schützt vor OOM auf Free-Tier.
# 1 Worker-Thread zieht Jobs sequenziell aus der Queue. Andere User warten in Position 2,3,...
import queue as _stdqueue
_calc_queue = _stdqueue.Queue()
_calc_running_id = None
_calc_worker_started = False
# Geschätzte Zeit pro Job in Sekunden (für ETA-Berechnung im Frontend)
_AVG_JOB_SECONDS = 150


def _get_queue_position(job_id):
    """Gibt die 1-basierte Queue-Position zurück. 1 = wird gerade gerechnet bzw als Nächstes.
    None wenn nicht in Queue (vermutlich schon fertig oder läuft gerade).
    Atomar gegen race conditions: wir snapshoten beide Werte unter Lock.
    """
    with _jobs_lock:
        running = _calc_running_id
        snap = list(_calc_queue.queue)
    if running == job_id:
        return 1
    pos = 1 if running else 0
    for entry in snap:
        if entry[0] == job_id:
            return pos + 1
        pos += 1
    return None


def _release_memory_to_os():
    """Forciert Python-Allocator Speicher tatsächlich an OS zurückzugeben (Linux/glibc).
    Standardmäßig hält Python freigegebenen RAM intern für Reuse — auf Render Free
    wirkt das dann nach OUTSIDE wie Memory-Leak. malloc_trim(0) räumt das auf."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass  # Mac/Windows oder kein libc — egal


def _calc_worker():
    """Background-Worker: pickt Jobs aus _calc_queue, führt sequenziell aus."""
    global _calc_running_id
    while True:
        try:
            job_id, form, files = _calc_queue.get()
        except Exception as e:
            print(f"[queue-worker] queue.get fail: {e}")
            continue
        try:
            with _jobs_lock:
                _calc_running_id = job_id
                if job_id in _jobs and _jobs[job_id].get('status') == 'queued':
                    _jobs[job_id]['status'] = 'pending'
            _run_process_async(job_id, form, files)
        except Exception as e:
            print(f"[queue-worker] job {job_id[:8]} crash: {e}")
        finally:
            with _jobs_lock:
                _calc_running_id = None
            try: _calc_queue.task_done()
            except: pass
            # Aggressive Memory-Freigabe nach jedem Job
            gc.collect()
            _release_memory_to_os()


def _restart_recovery_async():
    """Läuft im Background-Thread, blockiert nicht den App-Start (sonst Render Port-Timeout)."""
    try:
        if not os.path.exists(_JOBS_DIR):
            return
        files_list = os.listdir(_JOBS_DIR)
        if not files_list:
            return
        recovered = 0
        for fn in files_list:
            if not fn.endswith('.json'): continue
            try:
                path = os.path.join(_JOBS_DIR, fn)
                with open(path) as _f:
                    j = json.load(_f)
                if j.get('status') in ('queued', 'pending', 'running'):
                    j['status'] = 'failed'
                    j['error']  = 'Server wurde neugestartet während die Auswertung lief. Bitte mit deinem Code (AT-...) erneut starten — keine erneute Zahlung nötig.'
                    j['restart_recovered'] = True
                    job_id = fn[:-5]
                    with _jobs_lock:
                        _jobs[job_id] = j
                    with open(path, 'w') as _wf:
                        json.dump(j, _wf, default=str)
                    recovered += 1
            except Exception as _re:
                print(f"[queue] Restart-Recovery fail für {fn}: {_re}")
        if recovered > 0:
            print(f"[queue] Restart-Recovery: {recovered} Job(s) auf 'failed' gesetzt")
    except Exception as _e:
        print(f"[queue] Restart-Recovery konnte JOBS_DIR nicht lesen: {_e}")


def _start_calc_worker():
    global _calc_worker_started
    if _calc_worker_started:
        return
    _calc_worker_started = True
    _T = __import__('threading').Thread
    # Worker-Thread (verarbeitet Queue) sofort starten
    _T(target=_calc_worker, daemon=True, name='calc-worker').start()
    # Restart-Recovery in eigenem Thread (kann lange dauern bei vielen alten Jobs)
    # → blockiert nicht den App-Start, Port wird sofort geöffnet
    _T(target=_restart_recovery_async, daemon=True, name='restart-recovery').start()
    print("[queue] Worker-Thread + Restart-Recovery gestartet (async)")


# Worker beim App-Init starten (bei Render-Worker-Start).
# Tests/Imports können die Threads deaktivieren, damit Unit-Tests wirklich isoliert bleiben.
if os.environ.get('AEROTAX_DISABLE_BG_THREADS') != '1':
    _start_calc_worker()


_PII_KEYS = {
    # Direkte Identifier (DSGVO Art. 4)
    'identnr', 'geburtsdatum', 'personalnummer',
    'name', 'vorname', 'nachname',
    'finanzamt', 'steuernummer_ag',
    'adresse', 'plz', 'ort', 'strasse',
    'iban', 'bic',
    'email', 'telefon', 'phone',
}


def _redact_pii(obj):
    """Rekursiv: ersetzt PII-Felder durch '[redacted]'. Nicht in-place — neue Struktur.
    Wird vor Disk-Persist genutzt damit job-state files keine personenbezogenen
    Daten enthalten (DSGVO-Compliance)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower().lstrip('_') in _PII_KEYS:
                out[k] = '[redacted]' if v else v
            else:
                out[k] = _redact_pii(v)
        return out
    if isinstance(obj, list):
        return [_redact_pii(x) for x in obj]
    return obj


def _save_job_to_disk(job_id):
    """v9.7: Speichert Job-State in Supabase (persistent über Render-Deploys/Restarts)
    + Disk-Fallback. PII wird redacted."""
    with _jobs_lock:
        j = _jobs.get(job_id, {}).copy()
    if not j: return
    j_safe = {k: v for k, v in j.items() if k != 'files'}
    j_safe = _redact_pii(j_safe)
    # Supabase-Persistenz (überlebt Render-Container-Wipes)
    if SB_AVAILABLE:
        try:
            sb.table('jobs').upsert({
                'job_id':     job_id,
                'data':       j_safe,
                'updated_at': datetime.utcnow().isoformat() + 'Z',
            }).execute()
        except Exception as e:
            print(f"[persist] Job {job_id[:8]} supabase save fail: {e} — fallback to disk")
    # Disk-Fallback (auch zusätzlich für lokale Dev)
    try:
        with open(os.path.join(_JOBS_DIR, f'{job_id}.json'), 'w') as f:
            json.dump(j_safe, f, default=str)
    except Exception as e:
        print(f"[persist] Job {job_id[:8]} disk save fail: {e}")


def _load_job_from_disk(job_id):
    """v9.7: Lädt Job-State zuerst aus Supabase (überlebt Restart),
    dann Disk-Fallback. Bei Erfolg auch ins _jobs-Memory-Dict spiegeln."""
    # 1. Supabase
    if SB_AVAILABLE:
        try:
            res = sb.table('jobs').select('data').eq('job_id', job_id).limit(1).execute()
            rows = (res and res.data) or []
            if rows and rows[0].get('data'):
                data = rows[0]['data']
                with _jobs_lock:
                    _jobs[job_id] = data
                return data
        except Exception as e:
            print(f"[persist] Job {job_id[:8]} supabase load fail: {e} — try disk")
    # 2. Disk-Fallback
    path = os.path.join(_JOBS_DIR, f'{job_id}.json')
    if not os.path.exists(path): return None
    try:
        with open(path) as f:
            data = json.load(f)
            with _jobs_lock:
                _jobs[job_id] = data
            return data
    except Exception as e:
        print(f"[persist] Job {job_id[:8]} disk load fail: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# v10.4 — job_chunks: persistente Chunk-Zwischenergebnisse für DP-Reader.
# Reduziert Memory-Pressure auf Render Free-Tier (chunks werden sofort gespeichert,
# große Sonnet-Responses sofort freigegeben).
# Disk-Fallback wenn Supabase nicht verfügbar.
# ══════════════════════════════════════════════════════════════════════════════

_job_chunks_memory = {}  # job_id → list of chunk dicts (in-memory fallback)
_JOB_CHUNKS_DIR = os.path.join(os.path.dirname(__file__), '_job_chunks_state')
try:
    os.makedirs(_JOB_CHUNKS_DIR, exist_ok=True)
except Exception:
    pass


def _sanitize_chunk_result(result):
    """v10.4: Stellt sicher dass NIEMALS PDF-Bytes oder base64 in result_json landen.
    Entfernt verdächtig große String-Felder und bekannte Binary-Keys."""
    if not isinstance(result, dict):
        return result
    BLOCKED_KEYS = {'file_bytes', 'file_bytes_list', 'pdf_bytes', 'data_b64',
                    'base64', 'b64', 'raw_pdf', 'pdf_content', 'image_bytes'}
    out = {}
    for k, v in result.items():
        if k in BLOCKED_KEYS:
            continue
        # String-Feld > 50 KB ist verdächtig (PDFs sind groß, normaler Text klein)
        if isinstance(v, str) and len(v) > 50_000:
            out[k] = f'<truncated:{len(v)} chars>'
        elif isinstance(v, dict):
            out[k] = _sanitize_chunk_result(v)
        elif isinstance(v, list):
            out[k] = [_sanitize_chunk_result(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
    return out


def create_job_chunk(job_id, document_type, chunk_index,
                     page_from=None, page_to=None,
                     file_hash=None, parser_version=None):
    """Erstellt einen pending-Chunk-Eintrag. Idempotent via (job_id, document_type, chunk_index).
    v10.4.1: file_hash + parser_version unterstützen Cache-Lookups bei Wiederholung
    derselben Datei.
    Returns chunk_id (UUID) oder None bei Fehler."""
    import uuid as _uuid
    chunk_id = str(_uuid.uuid4())
    row = {
        'id': chunk_id,
        'job_id': str(job_id),
        'document_type': document_type,
        'chunk_index': int(chunk_index),
        'page_from': page_from,
        'page_to': page_to,
        'file_hash': file_hash,
        'parser_version': parser_version,
        'status': 'pending',
        'result_json': None,
        'error_code': None,
        'error_message': None,
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'updated_at': datetime.utcnow().isoformat() + 'Z',
    }
    if SB_AVAILABLE:
        try:
            sb.table('job_chunks').upsert(
                row, on_conflict='job_id,document_type,chunk_index'
            ).execute()
            return chunk_id
        except Exception as e:
            print(f"[chunks] create supabase fail {job_id[:8]}/{document_type}/{chunk_index}: {str(e)[:120]}")
    # Disk-Fallback
    _job_chunks_memory.setdefault(job_id, []).append(row)
    try:
        path = os.path.join(_JOB_CHUNKS_DIR, f'{job_id}.json')
        with open(path, 'w') as f:
            json.dump(_job_chunks_memory[job_id], f)
    except Exception:
        pass
    return chunk_id


def mark_job_chunk_running(chunk_id):
    """Markiert chunk als 'running'."""
    _update_chunk_fields(chunk_id, status='running')


def save_job_chunk_result(chunk_id, result_json):
    """Speichert Chunk-Result + setzt status='completed'.
    result_json wird sanitized (kein base64/PDF-bytes)."""
    sanitized = _sanitize_chunk_result(result_json)
    _update_chunk_fields(chunk_id, status='completed', result_json=sanitized,
                         error_code=None, error_message=None)


def mark_job_chunk_failed(chunk_id, error_code, error_message):
    """Markiert chunk als failed mit Code + Message."""
    _update_chunk_fields(chunk_id, status='failed',
                         error_code=str(error_code)[:50],
                         error_message=str(error_message)[:500])


def _update_chunk_fields(chunk_id, **fields):
    """Internes Update — auf Supabase + Disk-Fallback."""
    fields['updated_at'] = datetime.utcnow().isoformat() + 'Z'
    if SB_AVAILABLE:
        try:
            sb.table('job_chunks').update(fields).eq('id', chunk_id).execute()
            return
        except Exception as e:
            print(f"[chunks] update supabase fail {chunk_id[:8]}: {str(e)[:120]}")
    # Disk-Fallback
    for job_id, chunks in _job_chunks_memory.items():
        for c in chunks:
            if c.get('id') == chunk_id:
                c.update(fields)
                try:
                    path = os.path.join(_JOB_CHUNKS_DIR, f'{job_id}.json')
                    with open(path, 'w') as f:
                        json.dump(chunks, f)
                except Exception:
                    pass
                return


def load_job_chunks(job_id, document_type=None, status=None):
    """Lädt alle Chunks für einen Job. Optional gefiltert nach document_type/status.
    Returns list of chunk dicts, sortiert nach chunk_index."""
    chunks = []
    if SB_AVAILABLE:
        try:
            q = sb.table('job_chunks').select('*').eq('job_id', str(job_id))
            if document_type:
                q = q.eq('document_type', document_type)
            if status:
                q = q.eq('status', status)
            res = q.execute()
            chunks = (res and res.data) or []
        except Exception as e:
            print(f"[chunks] load supabase fail {str(job_id)[:8]}: {str(e)[:120]}")
    if not chunks:
        # Disk-Fallback
        path = os.path.join(_JOB_CHUNKS_DIR, f'{job_id}.json')
        if os.path.exists(path):
            try:
                chunks = json.load(open(path))
            except Exception:
                chunks = []
        if not chunks and job_id in _job_chunks_memory:
            chunks = _job_chunks_memory[job_id]
        if document_type:
            chunks = [c for c in chunks if c.get('document_type') == document_type]
        if status:
            chunks = [c for c in chunks if c.get('status') == status]
    chunks.sort(key=lambda c: c.get('chunk_index', 0))
    return chunks


def find_cached_chunk(file_hash, document_type, chunk_index, parser_version):
    """v10.4.1: Cache-Lookup für completed chunks.
    Wenn dieselbe Datei (SHA-256) + selbe parser_version bereits ausgewertet:
    Result direkt aus Supabase ohne Sonnet-Call.

    Returns result_json (dict) oder None.
    """
    if not file_hash or not parser_version:
        return None
    if SB_AVAILABLE:
        try:
            res = sb.table('job_chunks').select('result_json').eq(
                'file_hash', file_hash).eq(
                'document_type', document_type).eq(
                'chunk_index', int(chunk_index)).eq(
                'parser_version', parser_version).eq(
                'status', 'completed').limit(1).execute()
            rows = (res and res.data) or []
            if rows:
                return rows[0].get('result_json')
        except Exception as e:
            print(f"[chunks] cache lookup fail: {str(e)[:120]}")
    # Disk-Fallback: scan all known chunks (rare path, slower)
    try:
        for fn in os.listdir(_JOB_CHUNKS_DIR):
            if not fn.endswith('.json'): continue
            try:
                chunks = json.load(open(os.path.join(_JOB_CHUNKS_DIR, fn)))
                for c in chunks:
                    if (c.get('file_hash') == file_hash and
                        c.get('document_type') == document_type and
                        c.get('chunk_index') == int(chunk_index) and
                        c.get('parser_version') == parser_version and
                        c.get('status') == 'completed'):
                        return c.get('result_json')
            except Exception:
                continue
    except Exception:
        pass
    return None


def cleanup_old_job_chunks():
    """Löscht job_chunks älter als 7 Tage. Wird im _cleanup_loop aufgerufen."""
    if not SB_AVAILABLE:
        # Disk-Fallback Cleanup
        try:
            cutoff = datetime.utcnow() - timedelta(days=7)
            for fn in os.listdir(_JOB_CHUNKS_DIR):
                if not fn.endswith('.json'): continue
                path = os.path.join(_JOB_CHUNKS_DIR, fn)
                try:
                    if datetime.utcfromtimestamp(os.path.getmtime(path)) < cutoff:
                        os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass
        return
    try:
        cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
        res = sb.table('job_chunks').delete().lt('updated_at', cutoff).execute()
        deleted = len((res and getattr(res, 'data', None)) or [])
        if deleted:
            print(f"[cleanup] supabase: {deleted} job_chunks (>7d) deleted")
    except Exception as e:
        print(f"[cleanup] job_chunks delete fail: {str(e)[:120]}")


def _audit(job_id, event, data=None):
    """Schreibt Audit-Event in Job-Log + Render-Stdout. Audit-konform für Tax-Compliance."""
    entry = {
        'ts': datetime.utcnow().isoformat() + 'Z',
        'event': event,
        'data': data,
    }
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].setdefault('audit', []).append(entry)
    print(f"[AUDIT {job_id[:8]}] {event}: {json.dumps(data, default=str)[:200] if data else ''}")


def _validate_file_categories(files):
    """Quick sanity check: peek in PDF-text und prüfe ob Files typisch zur Kategorie passen.
    Liefert Liste warnings (leer = alles ok). Verhindert teure KI-Calls bei
    offensichtlich falschen Uploads (z.B. User lädt Reisepass statt LSB hoch)."""
    warnings = []
    # Marker-Strings die typisch in der Kategorie vorkommen (case-insensitive)
    markers = {
        'lsb':     ['lohnsteuerbescheinigung', 'bruttoarbeitslohn', 'arbeitslohn',
                    'bescheinigungsnummer', 'einbehaltene lohnsteuer'],
        'se':      ['streckeneinsatz', 'spesen', 'stfrei', 'steuerfrei',
                    'abrechnung', 'verpflegung'],
        'dp':      ['dienstplan', 'duty', 'roster', 'flug', 'frei'],
        'einsatz': ['einsatzplan', 'briefing', 'cas-pub', 'rotation', 'duty'],
    }
    label_de = {'lsb': 'Lohnsteuerbescheinigung', 'se': 'Streckeneinsatz',
                'dp': 'Dienstplan', 'einsatz': 'Einsatzplan'}
    for cat, kw_list in markers.items():
        items = files.get(cat) or []
        for idx, item in enumerate(items):
            pdf_bytes = item[0] if isinstance(item, tuple) else item
            if not pdf_bytes or not isinstance(pdf_bytes, (bytes, bytearray)):
                continue
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    # Erste Seite reicht für Sanity — sparsam mit Memory
                    page1_text = (pdf.pages[0].extract_text() or '') if pdf.pages else ''
            except Exception:
                continue  # nicht lesbar — Plausi überspringen, KI versucht's
            ptext = page1_text.lower()
            if not any(kw in ptext for kw in kw_list):
                warnings.append(
                    f'⚠ Datei {idx+1} in Kategorie "{label_de[cat]}" enthält keine '
                    f'typischen Begriffe (z.B. "{kw_list[0]}"). Bitte prüfen ob du das '
                    f'richtige Dokument hochgeladen hast.'
                )
    return warnings


@app.route('/api/process', methods=['POST'])
def process_real():
    """Startet asynchrone Auswertung. Liefert sofort job_id, Frontend pollt /api/job/<id>."""
    try:
        # Rate-Limit pro IP: max 20 Process-Calls/h. Stripe ist primärer Anti-Abuse-Layer,
        # IP-Limit fängt nur Versuche das Endpoint zu spammen (z.B. Promo-Code-Brute-Force).
        ip = _client_ip()
        if _ip_rate_limited(ip, 'process', limit=20, window_sec=3600):
            return jsonify({
                'error': 'Zu viele Versuche von dieser Verbindung. Bitte warte ca. 1 Stunde und versuche es dann erneut.'
            }), 429
        anreise = request.form.get('anreise', 'auto')
        # Robuste Number-Casts mit Fallback bei fehlerhaftem Input
        def _safe_int(v, default):
            try: return int(float(v))
            except: return default
        def _safe_float(v, default=0.0):
            try: return float(v)
            except: return default
        year_input = _safe_int(request.form.get('year', 2025), 2025)
        # Year auf supportete Range klemmen (2023-2026)
        year_input = max(2023, min(2026, year_input))

        # Sanity-Caps für User-Eingaben (verhindert absurde Werte)
        km_raw = _safe_float(request.form.get('km', 0))
        km_capped = max(0.0, min(500.0, km_raw))  # 0-500 km plausible
        anfahrt_raw = _safe_int(request.form.get('anfahrt_min', 0), 0)
        anfahrt_capped = max(0, min(180, anfahrt_raw))  # 0-180 min plausible
        oepnv_raw = _safe_float(request.form.get('oepnv_kosten', 0))
        oepnv_capped = max(0.0, min(10000.0, oepnv_raw))
        shuttle_raw = _safe_float(request.form.get('shuttle_kosten', 0))
        shuttle_capped = max(0.0, min(10000.0, shuttle_raw))
        # Ausfallzeit (Mutterschutz/Krankheit/Teilzeit) — Monate die nicht gearbeitet wurde
        ausfall_raw = _safe_int(request.form.get('ausfallzeit_monate', 0), 0)
        ausfall_capped = max(0, min(12, ausfall_raw))

        # Anreise: kann Multi-Mode sein (CSV: "auto,shuttle"), bleibt für Single-Mode kompatibel
        # km nur relevant wenn auto/fahrrad in den Modes
        anreise_modes_raw = set(m.strip() for m in str(anreise).split(',') if m.strip())
        has_km = bool(anreise_modes_raw & {'auto', 'fahrrad'})

        form = {
            'name':    request.form.get('name', 'Flugbegleiter'),
            'vorname': request.form.get('vorname', ''),
            'nachname':request.form.get('nachname', ''),
            'year':    year_input,
            'base':    request.form.get('base', 'Frankfurt (FRA)'),
            'anreise': anreise,
            'km':      km_capped if has_km else 0,
            'fahrzeug':   request.form.get('fahrzeug', 'verbrenner'),
            'oepnv_kosten': oepnv_capped,
            'shuttle_kosten': shuttle_capped,
            'jobticket':  request.form.get('jobticket', 'nein'),
            'anfahrt_min': anfahrt_capped,
            'ausfallzeit_monate': ausfall_capped,
        }

        files = {}
        for key in _ALL_FILE_KEYS:
            uploaded = request.files.getlist(key)
            if uploaded:
                files[key] = [_normalize_upload(f.read(), f.filename) for f in uploaded]

        # Audit: Wieviele Files kamen direkt im Request an?
        # Pflicht-Kategorien (lsb/dp=flugstunden/se) explizit, Rest nur wenn vorhanden
        direct_parts = []
        for k, v in files.items():
            if not v:
                continue
            label = {'dp': 'flugstunden', 'se': 'streckeneinsatz', 'lsb': 'lsb'}.get(k, k)
            direct_parts.append(f"{label}={len(v)}")
        print(f"[process] Direct-Upload: {', '.join(direct_parts) or 'KEINE'}")

        # Fallback: Files aus _store (in-memory) — überlebt Stripe-Retry
        # v11: Pflicht-Set = LSB + SE + CAS (kein DP mehr)
        ref_for_fallback = (request.form.get('ref') or '').strip()
        if (not files.get('lsb') or not files.get('se') or not files.get('cas')) \
                and ref_for_fallback and _store.get(ref_for_fallback, {}).get('files'):
            stored = _store[ref_for_fallback]['files']
            for k, items in stored.items():
                if k in files:
                    continue
                files[k] = [it[0] if isinstance(it, tuple) else it for it in items]
            print(f"[process] ref={ref_for_fallback[:8]} Files aus _store geladen ({sum(len(v) for v in files.values())} insgesamt)")

        # Letzter Fallback: Supabase — überlebt Render-Restart
        if (not files.get('lsb') or not files.get('se') or not files.get('cas')) \
                and ref_for_fallback:
            sb_files = _load_uploaded_files_supabase(ref_for_fallback)
            if sb_files:
                for k, items in sb_files.items():
                    if k in files:
                        continue
                    files[k] = [d for (d, _) in items]
                print(f"[process] ref={ref_for_fallback[:8]} Files aus Supabase geladen ({sum(len(v) for v in files.values())} insgesamt)")

        # v11: CAS ist neues 3. Pflicht-Dokument. Wenn nur DP (Flugstundenübersicht) hochgeladen
        # wurde, friendly weisen — User soll CAS hochladen.
        if files.get('dp') and not files.get('cas'):
            return jsonify({
                'error': 'Die Flugstundenübersicht wird im neuen Ablauf nicht mehr benötigt. '
                         'Bitte lade stattdessen deinen Dienstplan/CAS/Roster hoch — '
                         'du findest ihn in MyTime → Document Store.'
            }), 400

        if not files.get('lsb') or not files.get('se') or not files.get('cas'):
            return jsonify({
                'error': 'Für die Auswertung brauchst du Lohnsteuerbescheinigung, '
                         'Streckeneinsatzabrechnung und Dienstplan/CAS/Roster.'
            }), 400

        # ── PAYMENT-GATE: ref (Stripe), free_retry_token, oder valider Promo-Code ──
        free_retry_token = (request.form.get('free_retry_token') or '').strip()
        ref = (request.form.get('ref') or '').strip()
        pi_id = (request.form.get('payment_intent_id') or '').strip()
        promo_code = (request.form.get('promo_code') or '').strip().upper()
        is_free_retry = _is_valid_recovery_token(free_retry_token)
        is_paid = bool(ref and _store.get(ref, {}).get('paid'))
        # Replay-Schutz: PI darf nur 1x für /api/process genutzt werden
        if pi_id and pi_id in _consumed_payment_intents:
            return jsonify({
                'error': 'Diese Zahlung wurde bereits für eine Auswertung verwendet. Pro Bezahlung gibt es eine Auswertung.'
            }), 402
        # Wenn Webhook noch nicht durch ist: PaymentIntent direkt bei Stripe verifizieren
        if not is_paid and pi_id:
            try:
                pi = stripe.PaymentIntent.retrieve(pi_id)
                pi_ref = ((pi.metadata or {}).get('ref') or '').strip() if pi else ''
                if pi and pi.status == 'succeeded' and pi_ref:
                    # WICHTIG: PaymentIntent darf nur die eigene Upload-ref freischalten.
                    # Sonst könnte ein bezahlter PI versehentlich/absichtlich für fremde refs genutzt werden.
                    if ref and ref != pi_ref:
                        return jsonify({'error': 'Zahlung passt nicht zu dieser Upload-Session.'}), 402
                    ref = ref or pi_ref
                    is_paid = True
                    if ref and ref in _store:
                        _store[ref]['paid'] = True
            except Exception as _e:
                print(f"[stripe] PI verify fail {pi_id[:12]}: {_e}")
        valid_promos = set(c.strip().upper() for c in os.environ.get('PROMO_CODES', 'AEROTAXFREEPASS26').split(',') if c.strip())
        is_promo = bool(promo_code and promo_code in valid_promos)
        allow_unpaid = os.environ.get('ALLOW_UNPAID') == '1'
        if not (is_paid or is_free_retry or is_promo or allow_unpaid):
            return jsonify({
                'error': 'Zahlung nicht verifiziert. Bitte schließe den Bezahlvorgang ab und versuche es dann erneut.'
            }), 402

        # ── ASYNC: Job anlegen, Token sofort generieren, im Hintergrund starten ──
        job_id = str(uuid.uuid4())
        if is_free_retry:
            session_token = free_retry_token
            # Recovery-Token verbrauchen — nur 1x pro Token zulässig
            _recovery_tokens.pop(free_retry_token, None)
            _save_session(session_token, {
                'job_id': job_id,
                'result_data': {},
                'notes': [],
                'download_url': None,
                'chat_history': [],
            })
        else:
            # Normaler Pfad: Token sofort erstellen, 24h gültig auch bei Fehler.
            session_token = _make_session_token(job_id)
            _save_session(session_token, {
                'job_id': job_id,
                'result_data': {},
                'notes': [],
                'download_url': None,
                'chat_history': [],
            })
            # Bezahlung verbrauchen — Ref + PI können nur 1x für /api/process genutzt werden
            if ref and ref in _store:
                _store[ref]['paid'] = False
                _store[ref]['used_at'] = datetime.utcnow().isoformat() + 'Z'
            if pi_id:
                _consumed_payment_intents[pi_id] = datetime.utcnow()
                # Alte Einträge >25h aufräumen
                cutoff_pi = datetime.utcnow() - timedelta(hours=25)
                for k, ts in list(_consumed_payment_intents.items()):
                    if ts < cutoff_pi:
                        _consumed_payment_intents.pop(k, None)
        # Tentative Status — wird nach Put auf Basis echter Position korrigiert
        with _jobs_lock:
            _jobs[job_id] = {
                'status':   'pending',  # vorläufig, wird gleich aktualisiert
                'progress': 0,
                'created':  datetime.utcnow().isoformat() + 'Z',
                'session_token': session_token,
            }
        # ref/pi_id ans form-dict heften
        form['ref'] = ref or ''
        form['pi_id'] = pi_id or ''
        # Audit: nur die 3 Pflicht-Kategorien zählen + optionale Belege
        _v7_files = {
            'lsb': len(files.get('lsb') or []),
            'streckeneinsatz': len(files.get('se') or []),
            'cas': len(files.get('cas') or []),
            'dp_legacy': len(files.get('dp') or []),  # v11: dp ist nicht mehr Pflicht — Legacy-Tracking
        }
        _audit(job_id, 'job_created', {'year': form['year'], 'base': form['base'], 'files': _v7_files})

        # WICHTIG: ZUERST auf Disk persistieren, DANN in Queue einreihen.
        # Sonst: Crash zwischen put und save → Job in Queue (geht bei Restart verloren)
        # aber NICHT in JOBS_DIR → Frontend pollt 404. Mit save-first: Job überlebt
        # Restart, Restart-Recovery markiert ihn als 'failed' mit klarem Hinweis.
        _save_job_to_disk(job_id)

        # Queue-Tiefe-Limit: bei zu vielen Jobs in der Queue sofort 503 zurück
        # (verhindert OOM bei Spike + lange Wartezeiten)
        MAX_QUEUE_DEPTH = 8  # ~20 Min Wartezeit max
        current_queue_size = _calc_queue.qsize() + (1 if _calc_running_id else 0)
        if current_queue_size >= MAX_QUEUE_DEPTH:
            with _jobs_lock:
                _jobs[job_id]['status'] = 'rejected'
                _jobs[job_id]['error']  = f'Server gerade überlastet ({current_queue_size} Auswertungen in Wartezeit). Bitte in 5-10 Min mit deinem Code (AT-...) erneut versuchen — keine erneute Zahlung nötig.'
            _save_job_to_disk(job_id)
            return jsonify({
                'job_id': job_id,
                'status': 'rejected',
                'error':  _jobs[job_id]['error'],
                'session_token': session_token,
            }), 503

        _calc_queue.put((job_id, form, files))
        queue_pos = _get_queue_position(job_id) or 1
        # Echten Status anhand Position setzen
        initial_status = 'queued' if queue_pos > 1 else 'pending'
        with _jobs_lock:
            _jobs[job_id]['status'] = initial_status
        _save_job_to_disk(job_id)

        return jsonify({
            'job_id': job_id,
            'status': initial_status,
            'queue_position': queue_pos,
            'eta_seconds':    max(0, (queue_pos - 1) * _AVG_JOB_SECONDS),
            'session_token': session_token,
            'poll_url': f'/api/job/{job_id}',
        })

    except Exception as e:
        print(f'Process error: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _run_process_async(job_id, form, files):
    """Background-Worker — führt die echte Auswertung durch, schreibt Ergebnis in _jobs."""
    try:
        with _jobs_lock:
            _jobs[job_id]['status'] = 'running'
            _jobs[job_id]['progress'] = 5
        _audit(job_id, 'calculation_started')

        # Plausi-Check: sind die Files typisch zur Kategorie?
        try:
            file_warnings = _validate_file_categories(files)
            if file_warnings:
                _audit(job_id, 'file_validation_warnings', {'warnings': file_warnings})
                print(f"[file-validation] {len(file_warnings)} Warnung(en): {file_warnings[0][:120]}")
        except Exception as e:
            file_warnings = []
            print(f"[file-validation] Skip wegen Fehler: {e}")

        # v10.4: job_id durchreichen für chunked DP-Pipeline (Supabase-Persistenz + Heartbeat)
        result = berechne(form, files, job_id=job_id)
        if isinstance(result, tuple):
            result = result[0]
        # File-Validation-Warnings vorne in Notes einfügen — User soll sie sehen
        if file_warnings:
            existing_notes = result.get('hinweise') or result.get('notes') or []
            if isinstance(existing_notes, list):
                # vorne anhängen damit prominent
                key = 'hinweise' if 'hinweise' in result else 'notes'
                result[key] = list(file_warnings) + existing_notes
        _audit(job_id, 'calculation_done', {
            'gesamt': result.get('gesamt'), 'netto': result.get('netto'),
            'verification': result.get('_verification'),
        })
        # Tag-für-Tag-Klassifikation getrennt loggen (für Debug/Validation,
        # nicht im /api/job-Status weil zu groß). Über /api/job/<id>/audit abrufbar.
        _tage_detail = result.get('_tage_detail') or []
        if _tage_detail or result.get('_klass_summary'):
            _audit(job_id, 'classification_detail', {
                'summary':  result.get('_klass_summary'),
                'nachweis': (result.get('_nachweis') or '')[:2000],
                'unklare_tage':     result.get('_unklare_tage'),
                'audit_notes':      result.get('_audit_notes') or [],
                'unresolved_days':  result.get('_unresolved_days') or [],
                'vma_unmapped_se':  result.get('_vma_unmapped_se') or [],
                'document_health':  result.get('_document_health') or {},
                'z77_audit':        result.get('_z77_audit') or {},
                # v8.6: Diagnose-Listen für Live-Audit
                'extra_fahrtage':         result.get('_extra_fahrtage') or [],
                'extra_arbeitstage':      result.get('_extra_arbeitstage') or [],
                'extra_hotelnaechte':     result.get('_extra_hotelnaechte') or [],
                'wrong_z72_candidates':   result.get('_wrong_z72_candidates') or [],
                'missing_z73_candidates': result.get('_missing_z73_candidates') or [],
                'missing_z76_candidates': result.get('_missing_z76_candidates') or [],
                'missing_deutschland_14_candidates': result.get('_missing_deutschland_14_candidates') or [],
                'aerotax_z76_dates_amounts':   result.get('_aerotax_z76_dates_amounts') or [],
                'rescues':                     result.get('_rescues') or [],
                'training_sequences':          result.get('_training_sequences') or [],
                'training_commute_candidates': result.get('_training_commute_candidates') or [],
                'office_z72_candidates':       result.get('_office_z72_candidates') or [],
                'missing_reader_days':         result.get('_missing_reader_days') or [],
                'hotel_candidate_issues':      result.get('_hotel_candidate_issues') or [],
                'bmf_missing':            result.get('_bmf_missing') or [],
                'iata_unknown':           result.get('_iata_unknown') or [],
                'versions': {
                    'app': APP_VERSION,
                    'engine': ENGINE_VERSION,
                    'readers': READER_VERSIONS,
                    'prompt': PROMPT_VERSION,
                    'bmf_data_year': int(form.get('year', 2025)),
                },
                'tage_detail': _tage_detail,
            })

        with _jobs_lock:
            _jobs[job_id]['progress'] = 90

        pdf_bytes = erstelle_pdf(result)
        token = str(uuid.uuid4())
        name = result['name'].replace(' ', '_')
        year = form.get('year', 2025)
        _save_pdf(token, pdf_bytes, f'AeroTax_Auswertung_{year}_{name}.pdf')
        _audit(job_id, 'pdf_created', {'token': token, 'size_kb': len(pdf_bytes)//1024})

        # ── DATENSCHUTZ: ALLE Originaldateien sofort aus dem Speicher + Supabase entfernen ──
        try:
            for k in list(files.keys()):
                files[k] = None
            files.clear()
        except: pass
        # In-Memory _store[ref].files auch leeren
        try:
            ref_used = (form.get('ref') if isinstance(form, dict) else '') or ''
            if ref_used and ref_used in _store and _store[ref_used].get('files'):
                _store[ref_used]['files'] = {}
            # Supabase uploaded_files für ref löschen
            if ref_used:
                _delete_uploaded_files_supabase(ref_used)
        except Exception as _de:
            print(f"[cleanup] partial fail: {_de}")
        # File-bytes aus Result auch entfernen (Belege-Bilder waren da für PDF-Embedding)
        for b in result.get('optionale_belege', []):
            b.pop('file_bytes_list', None)
        _audit(job_id, 'files_purged', {'note': 'Originaldokumente sofort nach PDF-Generierung gelöscht (RAM + Supabase)'})

        # Filter: Felder die zum Frontend gehen — Dicts/Listen behalten
        # (außer file_bytes_list die explizit ausgeschlossen wird, da Bytes nicht JSON-serializable)
        def _is_jsonable(v):
            if isinstance(v, (int, float, str, bool)) or v is None:
                return True
            if isinstance(v, (list, tuple)):
                return all(_is_jsonable(x) for x in v)
            if isinstance(v, dict):
                return all(isinstance(k, str) and _is_jsonable(x) for k, x in v.items())
            return False
        safe = {k: v for k, v in result.items()
                if k != 'optionale_belege' and _is_jsonable(v)}
        opt_belege_safe = []
        for b in result.get('optionale_belege', []):
            b_safe = {k: v for k, v in b.items() if k != 'file_bytes_list'}
            opt_belege_safe.append(b_safe)

        # Form-Inputs für Edit-Restore mit-speichern (alle User-Eingaben)
        safe['_form_inputs'] = {
            'name':           form.get('name', ''),
            'vorname':        form.get('vorname', ''),
            'nachname':       form.get('nachname', ''),
            'year':           form.get('year', 2025),
            'base':           form.get('base', 'Frankfurt (FRA)'),
            'anreise':        form.get('anreise', 'auto'),
            'km':             form.get('km', 0),
            'fahrzeug':       form.get('fahrzeug', 'verbrenner'),
            'oepnv_kosten':   form.get('oepnv_kosten', 0),
            'shuttle_kosten': form.get('shuttle_kosten', 0),
            'jobticket':      form.get('jobticket', 'nein'),
            'anfahrt_min':    form.get('anfahrt_min', 0),
        }
        safe['_consumed_at'] = None  # wird beim PDF-Download gesetzt

        # Session-Token wurde bereits beim job_created erstellt — jetzt nur Result reinpacken
        with _jobs_lock:
            session_token = _jobs[job_id].get('session_token')
        # v8.22 Rest-1: kurzer ATX-Code zusätzlich zum Token
        short_code = _make_short_code(session_token) if session_token else ''
        safe['short_code'] = short_code
        if session_token:
            _save_session(session_token, {
                'job_id': job_id,
                'result_data': safe,
                'notes': result.get('notes', []),
                'download_url': f'/api/download/{token}',
                'short_code': short_code,
                'chat_history': [],
            })
            # Verknüpfung dl_token → session_token, damit Download den Token consumen kann
            _store.setdefault(token, {})['session_token'] = session_token

        with _jobs_lock:
            _jobs[job_id] = {
                **_jobs[job_id],
                'status':       'done',
                'progress':     100,
                'completed':    datetime.utcnow().isoformat() + 'Z',
                'download_url': f'/api/download/{token}',
                'data':         safe,
                'abrechnungen': result.get('abrechnungen', []),
                'optionale_belege': opt_belege_safe,
                'notes':        result.get('notes', []),
            }

    except Exception as e:
        import traceback
        traceback.print_exc()
        _audit(job_id, 'calculation_failed', {'error': str(e)})
        # Session-Token bleibt gültig — User kann mit demselben Token einfach erneut auswerten
        with _jobs_lock:
            session_token = _jobs[job_id].get('session_token', '')
            _jobs[job_id] = {
                **_jobs[job_id],
                'status':   'failed',
                'error':    str(e),
                'completed': datetime.utcnow().isoformat() + 'Z',
            }
        _save_job_to_disk(job_id)
        print(f"[FAIL {job_id[:8]}] Job failed. Session-Token bleibt gültig: {session_token}")
    finally:
        # Erfolg oder Fehler: persistiere Status nach Disk
        _save_job_to_disk(job_id)


@app.route('/api/job/<job_id>', methods=['GET'])
def get_job_status(job_id):
    """Pollt Status. Bei Memory-Miss: lade vom Disk (Server-Restart-Resilience)."""
    with _jobs_lock:
        j = _jobs.get(job_id)
    if not j:
        j = _load_job_from_disk(job_id)
        if j:
            with _jobs_lock:
                _jobs[job_id] = j
    if not j:
        return jsonify({'status': 'not_found'}), 404
    safe = {k: v for k, v in j.items() if k != 'audit'}
    # Wenn Job noch in Queue: aktuelle Position + ETA mitschicken
    status = safe.get('status')
    if status in ('queued', 'pending'):
        pos = _get_queue_position(job_id)
        if pos is not None:
            safe['queue_position'] = pos
            safe['eta_seconds']    = max(0, (pos - 1) * _AVG_JOB_SECONDS)
    return jsonify(safe)


@app.route('/api/job/<job_id>/audit', methods=['GET'])
def get_job_audit(job_id):
    """Vollständiges Audit-Log (für Compliance / Steuerberater)."""
    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
    if not j:
        return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
    return jsonify({'audit': j.get('audit', []), 'status': j.get('status')})


def _recompute_totals_from_cls(cls_new, cached_state, year):
    """v8.22 Pure-Helper: berechnet Topf-Trennung + Brutto/Netto aus neuer Klassifikation
    + gecachten Inputs (LSB, SE-Summe, Form). Identisch zur Logik in _berechne_via_hybrid.
    Liefert Dict mit allen relevanten Feldern für den Result-Dict-Update.
    """
    bmf_inland = BMF_INLAND_BY_YEAR.get(year, BMF_INLAND_BY_YEAR.get(2025, {
        'tagestrip_8h': 14, 'an_abreise': 14, 'voll_24h': 28
    }))
    reinig_satz = REINIGUNG_PRO_TAG_BY_YEAR.get(year, 1.60)
    trink_satz = TRINKGELD_PRO_NACHT_BY_YEAR.get(year, 3.60)
    pendler = PENDLER_BY_YEAR.get(year, PENDLER_BY_YEAR.get(2025, {'lt_20km': 0.30, 'gt_21km': 0.38}))

    fahr_tage_n     = int(cls_new.get('fahr_tage', 0) or 0)
    reinigungstage_n = int(cls_new.get('reinigungstage', cls_new.get('arbeitstage', 0)) or 0)
    hotel_n         = int(cls_new.get('hotel_naechte', 0) or 0)
    arbeitstage_n   = int(cls_new.get('arbeitstage', 0) or 0)
    vma_72_tage     = int(cls_new.get('z72_tage', 0) or 0)
    vma_73_tage     = int(cls_new.get('z73_tage', 0) or 0)
    vma_74_tage     = int(cls_new.get('z74_tage', 0) or 0)
    vma_aus         = float(cls_new.get('z76_eur', 0) or 0)

    vma_72 = round(vma_72_tage * bmf_inland['tagestrip_8h'], 2)
    vma_73 = round(vma_73_tage * bmf_inland['an_abreise'], 2)
    vma_74 = round(vma_74_tage * bmf_inland['voll_24h'], 2)
    vma_in = round(vma_72 + vma_73 + vma_74, 2)

    km = float(cached_state.get('km', 0) or 0)
    fahr = 0.0
    f_entfernungspauschale = 0.0
    if km > 0 and fahr_tage_n > 0:
        f_entfernungspauschale = round(
            min(km, 20) * fahr_tage_n * pendler['lt_20km']
            + max(0, km - 20) * fahr_tage_n * pendler['gt_21km'], 2)
        fahr += f_entfernungspauschale
    f_oepnv = float(cached_state.get('fahr_oepnv', 0) or 0)
    f_shuttle = float(cached_state.get('fahr_shuttle', 0) or 0)
    fahr += f_oepnv + f_shuttle
    fahr = round(fahr, 2)

    reinig = round(reinigungstage_n * reinig_satz, 2)
    trink  = round(hotel_n * trink_satz, 2)

    opt_zu_gesamt = float(cached_state.get('opt_zu_gesamt', 0) or 0)
    ag_z17        = float(cached_state.get('ag_z17', 0) or 0)
    z77           = float(cached_state.get('z77', 0) or 0)

    gesamt    = round(fahr + reinig + trink + vma_in + vma_aus + opt_zu_gesamt, 2)
    vma_total = round(vma_in + vma_aus, 2)
    vma_netto = round(max(0, vma_total - z77), 2)
    fahr_netto = round(max(0, fahr - ag_z17), 2)
    netto      = round(fahr_netto + reinig + trink + vma_netto + opt_zu_gesamt, 2)

    return {
        'arbeitstage':    arbeitstage_n,
        'reinigungstage': reinigungstage_n,
        'fahr_tage':      fahr_tage_n,
        'hotel_naechte':  hotel_n,
        'vma_72_tage':    vma_72_tage,
        'vma_73_tage':    vma_73_tage,
        'vma_74_tage':    vma_74_tage,
        'vma_72':         vma_72,
        'vma_73':         vma_73,
        'vma_74':         vma_74,
        'vma_in':         vma_in,
        'vma_aus':        vma_aus,
        'fahr':           fahr,
        'fahr_entfernungspauschale': f_entfernungspauschale,
        'fahr_oepnv':     f_oepnv,
        'fahr_shuttle':   f_shuttle,
        'reinig':         reinig,
        'trink':          trink,
        'gesamt':         gesamt,
        'netto':          netto,
        'ag_z17':         ag_z17,
        'z77':            z77,
    }


def _recompute_with_overrides(cached_state, manual_day_overrides):
    """v8.22: Wendet User-Review-Overrides auf gecachte matched_days an,
    klassifiziert neu, rechnet Topf-Totals neu. KEIN Sonnet-Call.

    cached_state braucht: matched_days, year, homebase, commute_minutes,
                          km, fahr_oepnv, fahr_shuttle, ag_z17, z77, opt_zu_gesamt
    """
    if not cached_state or not isinstance(cached_state, dict):
        return None
    matched = cached_state.get('matched_days') or []
    overrides = manual_day_overrides or {}
    # Patche DP-Felder pro Tag gemäß Override
    patched = []
    for m in matched:
        if not isinstance(m, dict):
            continue
        m_new = dict(m)
        dp = dict(m_new.get('dp') or {})
        ov = overrides.get(m_new.get('datum'))
        if ov:
            if 'start_time' in ov and 'end_time' in ov:
                st, et = ov['start_time'], ov['end_time']
                try:
                    sh, sm = int(st.split(':')[0]), int(st.split(':')[1])
                    eh, em = int(et.split(':')[0]), int(et.split(':')[1])
                    duration = (eh * 60 + em) - (sh * 60 + sm)
                    if duration > 0:
                        dp['start_time'] = st
                        dp['end_time'] = et
                        dp['duty_duration_minutes'] = duration
                        dp['time_is_absence'] = True
                        dp['_user_review_source'] = ov.get('source', 'user_review_chatbot_time_entry')
                except (ValueError, IndexError):
                    pass
            elif 'over_8h' in ov:
                if ov['over_8h']:
                    dp['duty_duration_minutes'] = SAME_DAY_Z72_TOTAL_MINUTES
                    dp['time_is_absence'] = True
                else:
                    dp['duty_duration_minutes'] = SAME_DAY_Z72_TOTAL_MINUTES - 1
                    dp['time_is_absence'] = True
                dp['_user_review_source'] = ov.get('source', 'user_review_chatbot')
            elif ov.get('unsure'):
                dp['_user_review_source'] = 'user_unsure'
        m_new['dp'] = dp
        patched.append(m_new)

    year = int(cached_state.get('year', 2025) or 2025)
    homebase = str(cached_state.get('homebase', 'FRA') or 'FRA').upper()
    commute_minutes = int(cached_state.get('commute_minutes', 0) or 0)
    cls_new = _deterministic_classify_v7(patched, year, homebase, commute_minutes=commute_minutes)
    totals = _recompute_totals_from_cls(cls_new, cached_state, year)
    return {'cls': cls_new, 'totals': totals}


def _validate_and_compute_review_answer(review_item_id, answer, start_time, end_time):
    """v8.21 Pure-Helper: Validiert Review-Antwort und berechnet Override + Delta.

    Returns:
        (200, {ov, delta_eur, delta_label, datum, type}) bei Erfolg
        (status_code, {error: ...}) bei Validierungsfehler
    """
    import re as _re_v
    review_item_id = (review_item_id or '').strip()
    answer = (answer or '').strip()
    start_time = (start_time or '').strip()
    end_time = (end_time or '').strip()

    if not review_item_id or ':' not in review_item_id:
        return 400, {'error': 'invalid review_item_id'}
    typ, datum = review_item_id.split(':', 1)
    if not datum:
        return 400, {'error': 'invalid datum in review_item_id'}
    if not _re_v.fullmatch(r'\d{4}-\d{2}-\d{2}', datum):
        return 400, {'error': 'datum muss ISO-Format YYYY-MM-DD haben'}
    if typ not in ('office_training_time_missing',):
        return 400, {'error': f'unbekannter review-type: {typ}'}

    delta_eur = 0.0
    delta_label = 'Keine Änderung am Betrag'
    if answer == 'yes':
        ov = {'over_8h': True, 'source': 'user_review_chatbot'}
        delta_eur = 14.0
        delta_label = '+14,00 € berücksichtigt'
    elif answer == 'no':
        ov = {'over_8h': False, 'source': 'user_review_chatbot'}
        delta_label = 'Okay, kein zusätzlicher Betrag für diesen Tag.'
    elif answer == 'time':
        if not start_time or not end_time:
            return 400, {'error': 'start_time and end_time required for time answer'}
        if not _re_v.fullmatch(r'([01]?\d|2[0-3]):[0-5]\d', start_time):
            return 400, {'error': 'start_time muss HH:MM sein (z.B. 08:30)'}
        if not _re_v.fullmatch(r'([01]?\d|2[0-3]):[0-5]\d', end_time):
            return 400, {'error': 'end_time muss HH:MM sein (z.B. 18:45)'}
        try:
            sh, sm = int(start_time.split(':')[0]), int(start_time.split(':')[1])
            eh, em = int(end_time.split(':')[0]), int(end_time.split(':')[1])
            duration = (eh * 60 + em) - (sh * 60 + sm)
        except (ValueError, IndexError):
            return 400, {'error': 'invalid time format (HH:MM)'}
        if duration <= 0:
            return 400, {
                'error': 'Endzeit muss nach Startzeit liegen. '
                         'Falls du wirklich über Mitternacht arbeitest, bitte separat prüfen.',
                'start_time': start_time, 'end_time': end_time,
            }
        if duration > 18 * 60:
            return 400, {
                'error': 'Abwesenheitsdauer >18h erscheint unplausibel — bitte prüfen.',
                'duration_minutes': duration,
            }
        ov = {
            'start_time': start_time, 'end_time': end_time,
            'time_is_absence': True,
            'source': 'user_review_chatbot_time_entry',
        }
        if duration >= SAME_DAY_Z72_TOTAL_MINUTES:
            delta_eur = 14.0
            delta_label = f'Erkannte Abwesenheit: {duration//60}:{duration%60:02d} h → +14,00 € berücksichtigt'
        else:
            delta_label = f'Erkannte Abwesenheit: {duration//60}:{duration%60:02d} h → kein zusätzlicher Betrag'
    elif answer == 'unsure':
        ov = {'unsure': True, 'source': 'user_unsure'}
        delta_label = 'Okay, ich lasse den Tag unverändert.'
    else:
        return 400, {'error': 'invalid answer (yes|no|time|unsure)'}

    return 200, {
        'datum': datum, 'type': typ,
        'override': ov, 'delta_eur': delta_eur, 'delta_label': delta_label,
    }


@app.route('/api/job/<job_id>/review-answer', methods=['POST'])
def post_review_answer(job_id):
    """v8.21: Speichert eine Nutzer-Antwort auf ein Review-Item.

    Body: {review_item_id, answer: 'yes'|'no'|'time'|'unsure',
           start_time?, end_time?, source?}

    Speichert das Override im Job-State (manual_day_overrides). Liefert
    eine deterministische Delta-Schätzung zurück (kein Sonnet-Call).
    Echte Re-Berechnung erfolgt bei /finalize-pdf (Phase 3b).
    """
    body = request.get_json(silent=True) or {}
    status, payload = _validate_and_compute_review_answer(
        review_item_id=body.get('review_item_id'),
        answer=body.get('answer'),
        start_time=body.get('start_time'),
        end_time=body.get('end_time'),
    )
    if status != 200:
        return jsonify(payload), status

    review_item_id = payload['type'] + ':' + payload['datum']
    datum = payload['datum']
    ov = payload['override']
    delta_eur = payload['delta_eur']
    delta_label = payload['delta_label']
    answer = body.get('answer', '').strip()

    # Job laden + Override speichern
    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        overrides = dict(j.get('manual_day_overrides') or {})
        overrides[datum] = ov
        j['manual_day_overrides'] = overrides
        # Audit-Log-Eintrag
        if 'audit' in j and isinstance(j['audit'], list):
            j['audit'].append({
                'event': 'review_answer',
                'data': {'review_item_id': review_item_id, 'answer': answer,
                         'datum': datum, 'override': ov, 'delta_eur': delta_eur},
                'timestamp': datetime.now().isoformat(),
            })

        # v8.22 Step C: Deterministische Re-Berechnung mit gecachtem State
        # (KEIN Sonnet-Call). Liefert authoritativen updated_preview_total.
        cached = (j.get('data') or {}).get('_cached_recalc_state') or {}
        updated_preview_total = None
        total_delta = None
        recalc_breakdown = None
        if cached and cached.get('matched_days'):
            try:
                rec = _recompute_with_overrides(cached, overrides)
                if rec and rec.get('totals'):
                    updated_preview_total = rec['totals'].get('netto')
                    initial_total = float(((j.get('data') or {}).get('netto')) or 0)
                    if isinstance(updated_preview_total, (int, float)):
                        total_delta = round(float(updated_preview_total) - initial_total, 2)
                    recalc_breakdown = {
                        'arbeitstage':    rec['totals'].get('arbeitstage'),
                        'reinigungstage': rec['totals'].get('reinigungstage'),
                        'fahr_tage':      rec['totals'].get('fahr_tage'),
                        'hotel_naechte':  rec['totals'].get('hotel_naechte'),
                        'vma_72_tage':    rec['totals'].get('vma_72_tage'),
                        'vma_73_tage':    rec['totals'].get('vma_73_tage'),
                        'vma_72':         rec['totals'].get('vma_72'),
                        'vma_73':         rec['totals'].get('vma_73'),
                        'vma_aus':        rec['totals'].get('vma_aus'),
                        'gesamt':         rec['totals'].get('gesamt'),
                        'netto':          rec['totals'].get('netto'),
                    }
                    # Persist updated preview totals in job data so UI/Refresh sehen es
                    j.setdefault('data', {})['_preview_totals'] = recalc_breakdown
            except Exception as _re:
                print(f'[review-answer] recalc fail: {_re}')

        try:
            _save_job_to_disk(job_id)
        except Exception as _e:
            print(f'[review-answer] save warning: {_e}')

    # v8.37: Session.result_data sync (Recall sieht post-review-Stand)
    try:
        _sync_session_result_with_job(job_id, j)
    except Exception as _e:
        print(f'[review-answer] session sync warn: {_e}')

    answered = sum(1 for v in overrides.values()
                   if isinstance(v, dict) and not v.get('unsure'))
    return jsonify({
        'review_item_id': review_item_id,
        'answer': answer,
        'delta_eur': delta_eur,
        'delta_label': delta_label,
        'override_saved': True,
        'answered_count': len(overrides),
        'answered_with_impact': answered,
        # v8.22 Step C: authoritative serverseitige Berechnung
        'updated_preview_total': updated_preview_total,
        'total_delta': total_delta,
        'preview_breakdown': recalc_breakdown,
    })


@app.route('/api/job/<job_id>/review-bulk-answer', methods=['POST'])
def post_review_bulk_answer(job_id):
    """v8.22 Now-3: Bulk-Antwort auf mehrere pending Review-Items vom selben Typ.

    Body: {answer: 'yes'|'no'|'unsure', type?: 'office_training_time_missing'}

    Wendet die Antwort auf alle pending Items des Typs an, source=user_bulk_review_chatbot.
    Liefert authoritative updated_preview_total via _recompute_with_overrides.
    """
    body = request.get_json(silent=True) or {}
    answer = (body.get('answer') or '').strip()
    typ = (body.get('type') or 'office_training_time_missing').strip()
    if answer not in ('yes', 'no', 'unsure'):
        return jsonify({'error': 'invalid bulk answer (yes|no|unsure)'}), 400
    if typ not in ('office_training_time_missing',):
        return jsonify({'error': f'unbekannter review-type: {typ}'}), 400

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        data = j.get('data') or {}
        review_items = list(data.get('_review_items') or [])
        existing_overrides = dict(j.get('manual_day_overrides') or {})

        # Override-Template
        if answer == 'yes':
            ov_template = {'over_8h': True, 'source': 'user_bulk_review_chatbot'}
        elif answer == 'no':
            ov_template = {'over_8h': False, 'source': 'user_bulk_review_chatbot'}
        else:
            ov_template = {'unsure': True, 'source': 'user_bulk_review_chatbot'}

        # Auf alle pending Items vom Typ anwenden
        applied_dates = []
        for it in review_items:
            if it.get('type') != typ:
                continue
            if it.get('status') == 'answered':
                continue  # bereits beantwortet, überspringen
            d_iso = it.get('datum')
            if not d_iso:
                continue
            existing_overrides[d_iso] = dict(ov_template)
            applied_dates.append(d_iso)

        j['manual_day_overrides'] = existing_overrides
        if 'audit' in j and isinstance(j['audit'], list):
            j['audit'].append({
                'event': 'review_bulk_answer',
                'data': {'answer': answer, 'type': typ,
                         'applied_count': len(applied_dates),
                         'applied_dates': applied_dates},
                'timestamp': datetime.now().isoformat(),
            })

        # Re-compute mit allen Overrides
        cached = data.get('_cached_recalc_state') or {}
        updated_preview_total = None
        total_delta = None
        if cached.get('matched_days'):
            try:
                rec = _recompute_with_overrides(cached, existing_overrides)
                if rec and rec.get('totals'):
                    updated_preview_total = rec['totals'].get('netto')
                    initial_total = float((data.get('netto')) or 0)
                    if isinstance(updated_preview_total, (int, float)):
                        total_delta = round(float(updated_preview_total) - initial_total, 2)
                    j.setdefault('data', {})['_preview_totals'] = rec['totals']
            except Exception as e:
                print(f'[review-bulk] recalc fail: {e}')
        try:
            _save_job_to_disk(job_id)
        except Exception as _e:
            print(f'[review-bulk] save warning: {_e}')

    # v8.37: Session-Sync für Recall-Persistenz
    try:
        _sync_session_result_with_job(job_id, j)
    except Exception as _e:
        print(f'[review-bulk] session sync warn: {_e}')

    return jsonify({
        'applied_count': len(applied_dates),
        'applied_dates': applied_dates,
        'answer': answer,
        'updated_preview_total': updated_preview_total,
        'total_delta': total_delta,
        'override_count': len(existing_overrides),
    })


# ── v8.26: Konversations-Endpoints für gruppierten Review-Flow ──

@app.route('/api/job/<job_id>/review-groups', methods=['GET'])
def get_review_groups(job_id):
    """v8.26: Liefert gruppierte review_items für Konversations-UX im Chat."""
    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        review_items = (j.get('data') or {}).get('_review_items') or []
    groups = _build_review_groups(review_items)
    return jsonify({'groups': groups, 'total_pending': sum(g['count'] for g in groups)})


def _build_ai_chat_context(job, session_data=None):
    """v9.1: Sicheren, vollständigen Job-Kontext für KI-Interpreter.
    Keine PII, keine Steuer-ID, kein Personalnummer.
    """
    data = (job.get('data') or {}) if job else {}
    review_items = data.get('_review_items') or []
    pending = [it for it in review_items if it.get('status') == 'pending']
    answered = [it for it in review_items if it.get('status') == 'answered']
    groups = _build_review_groups(review_items) if review_items else []

    # Compact group summary for AI
    group_summary = []
    for g in groups:
        group_summary.append({
            'group_id':       g.get('group_id'),
            'label':          g.get('label'),
            'date_range':     g.get('date_range'),
            'count':          g.get('count'),
            'item_ids':       g.get('item_ids', []),
            'datums':         g.get('datums', []),
            'marker_summary': g.get('marker_summary'),
            'group_type':     g.get('group_type'),
        })

    return {
        'tax_year':        data.get('year'),
        'airline':         data.get('arbeitgeber', 'Lufthansa'),
        'current_total':   float(data.get('netto') or 0),
        'brutto':          float(data.get('brutto') or 0),
        'lohnsteuer':      float(data.get('lohnsteuer') or 0),
        'fahr':            float(data.get('fahr') or 0),
        'fahr_tage':       int(data.get('fahr_tage') or 0),
        'reinig':          float(data.get('reinig') or 0),
        'reinigungstage':  int(data.get('reinigungstage') or data.get('arbeitstage') or 0),
        'trink':           float(data.get('trink') or 0),
        'hotel_naechte':   int(data.get('hotel_naechte') or 0),
        'z17':             float(data.get('ag_z17') or 0),
        'z77':             float(data.get('z77') or 0),
        'vma_72_tage':     int(data.get('vma_72_tage') or 0),
        'vma_73_tage':     int(data.get('vma_73_tage') or 0),
        'vma_74_tage':     int(data.get('vma_74_tage') or 0),
        'vma_aus':         float(data.get('vma_aus') or 0),
        'open_review_count':     len(pending),
        'answered_review_count': len(answered),
        'review_groups':         group_summary,
        'pending_review_items':  [{
            'id': it['id'],
            'datum': it.get('datum'),
            'marker': it.get('marker', ''),
            'type': it.get('type'),
        } for it in pending[:50]],  # cap at 50 to keep prompt small
        'pdf_status':            'pending_reread' if job.get('pending_reread') else (
                                  'ready' if data.get('pdf_finalized') else 'open'),
        'has_review_items':      bool(pending),
        'allowed_actions': [
            'review_answer', 'bulk_review', 'clarification',
            'document_upload', 'wiso_help', 'pdf_help',
            'rechenweg_help', 'zugangscode_help',
        ],
    }


_AI_SYSTEM_PROMPT = """Du bist der AeroTAX-Auswertungs-Assistent.

Du hilfst Lufthansa-Crew-Mitgliedern bei ihrer Werbungskosten-Auswertung — nur in diesem
Kontext. Du bist KEINE Steuerberatung.

Du bekommst:
- den vollständigen Job-Kontext (Beträge, offene Tage, Gruppen, Marker)
- die bisherigen letzten Chat-Turns
- die neue Nutzer-Nachricht

Marker-Glossar (Lufthansa Crew):
- D4 = Schulung
- EK = Bürodienst
- SM = Seminar
- EH = Erste-Hilfe-Schulung
- EM = Emergency-Training
- SIM = Simulator
Wenn der User „em", „eh", „d4" usw. schreibt → das sind die Marker, NICHT nachfragen.

Deine Aufgaben:
1. Nutzer-Absicht verstehen (auch chaotische Eingaben).
2. Antworten auf offene Tage interpretieren — Bulk, Datum, Gruppe, Marker, Monat.
3. Nur Tatsachen-Fragen stellen: „Warst du inkl. Hin- und Rückweg länger als 8 Stunden weg?"
4. Bei Bulk/komplexen Eingaben Zusammenfassung zeigen + Bestätigung anfordern.
5. WISO-/PDF-/Rechenweg-/Zugangscode-Fragen kurz beantworten.
6. Off-topic höflich blockieren.
7. NIEMALS Beträge selbst berechnen. Backend macht das.
8. NIEMALS steuerliche Garantien, Marketing, „mehr rausholen", „Netto in WISO", „Finanzamt akzeptiert".
9. Keine Markdown-Tabellen, keine Trennlinien (---), keine `**Header**`, keine emoji-bullet-lists.

ANTWORT-FORMAT (ZWINGEND):
Antworte AUSSCHLIESSLICH mit einem JSON-Objekt — kein Text davor oder danach.
Schema:
{
  "intent": "review_answer" | "bulk_review" | "clarification" | "question_answer" | "document_upload" | "pdf_action" | "off_topic",
  "message_to_user": "max 4 kurze Sätze, freundlich, deutsch",
  "needs_confirmation": true | false,
  "proposed_changes": [
    { "review_item_id": "<exakter id-string aus pending_review_items>",
      "answer": "yes" | "no" | "unsure",
      "reason": "kurz" }
  ],
  "clarification_question": null | "wenn intent=clarification: konkrete Frage",
  "next_action": "ask_confirmation" | "ask_clarification" | "answer_only" | "show_upload" | "block",
  "referenced_groups": ["g1", "g2"]  // Group-IDs auf die sich der User bezieht
}

REGELN:
- intent='bulk_review' → needs_confirmation MUSS true sein.
- proposed_changes nur mit review_item_ids aus pending_review_items.
- answer nur 'yes' | 'no' | 'unsure'.
- Bei klarer Bulk-Absicht („alle über 8h"): proposed_changes für ALLE pending_review_items.
- Bei „alle außer X" / „rest 0": proposed_changes für ALLE OUTSIDE der Exception.
- Bei „April ja, September nein": jedes Pending im April → yes, jedes Pending im September → no.
- Bei reiner Frage („wo finde ich..."): intent='question_answer', proposed_changes=[], next_action='answer_only'.
- Bei off-topic („wer ist Britney"): intent='off_topic', message_to_user=Standard-Block-Antwort.
- Bei Upload-Wunsch („ich lade plan hoch"): intent='document_upload', next_action='show_upload'.
- Bei Unklarheit: intent='clarification', clarification_question gesetzt, proposed_changes=[].

MULTI-TURN-REGEL (KRITISCH):
- Wenn dein vorheriger Bot-Turn eine konkrete Klärungsfrage gestellt hat (z.B.
  „Welche 2 Tage waren nicht über 8 Stunden?" oder „Welche Startzeit?")
  UND die neue Nutzer-Nachricht eine direkte Antwort darauf ist (Datum, Uhrzeit, Anzahl)
  → JETZT generierst du proposed_changes mit der vollständigen Bulk-Logik.
  Du fragst NICHT erneut. Du sammelst die Antwort + den vorherigen Bulk-Kontext zusammen
  und lieferst proposed_changes für den vollständigen Apply.

BEISPIEL Multi-Turn:
  User Turn 1: „alle über 8 außer 2"
  Bot Turn 1: „Welche 2 Tage waren nicht über 8h?" (clarification)
  User Turn 2: „29.04 und 13.05"
  Bot Turn 2 (DU JETZT): proposed_changes mit ALLEN pending außer 29.04+13.05 als 'yes',
                         29.04+13.05 als 'no'. needs_confirmation=true.

REGEL „Betrag-Auskunft":
- Du darfst NIEMALS eigenständig sagen, dass sich der Betrag ändert oder nicht ändert.
- Backend rechnet. Wenn der User „was ändert sich" fragt: setze intent='question_answer',
  message_to_user='Ich übernehme das gleich und das Backend berechnet den genauen Betrag.'
"""


@app.route('/api/job/<job_id>/ai-chat', methods=['POST'])
def post_ai_chat(job_id):
    """v9.1: KI-Interpreter mit vollem Job-Kontext + strukturiertes JSON.

    Body: {message, chat_history?: [{role, content}, ...]}
    Returns: AI-Response-JSON (siehe Schema im System-Prompt) +
             confirmation_id + estimated_delta wenn proposed_changes.
    Fallback bei Sonnet-Fehler: deterministischer Regex-Parser (v8.26).
    """
    import json as _json
    import re as _re
    import hashlib

    body = request.get_json(silent=True) or {}
    user_msg = (body.get('message') or '').strip()[:2000]
    if not user_msg:
        return jsonify({'error': 'message erforderlich'}), 400

    # Off-Topic-Schnell-Filter (kein KI-Call nötig)
    if _is_off_topic_question(user_msg):
        return jsonify({
            'intent': 'off_topic',
            'message_to_user': 'Ich kann dir hier nur bei deiner AeroTAX-Auswertung helfen — '
                                'also bei Unterlagen, offenen Punkten, PDF und der Übernahme in WISO.',
            'needs_confirmation': False,
            'proposed_changes': [],
            'next_action': 'block',
            'referenced_groups': [],
        })

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Auswertung nicht gefunden — bitte Seite neu laden.'}), 404
        review_items = (j.get('data') or {}).get('_review_items') or []
        items_by_id = {it['id']: it for it in review_items}
        cached = (j.get('data') or {}).get('_cached_recalc_state') or {}
        existing_overrides = dict(j.get('manual_day_overrides') or {})
        initial_total = float((j.get('data') or {}).get('netto') or 0)
        ctx = _build_ai_chat_context(j)

    # v9.2: Volle Chat-History (last 6 turns), nicht 300-char-truncated — sonst geht Kontext verloren
    chat_history = (body.get('chat_history') or [])[-6:]
    history_block = '\n\n'.join(
        f"{('USER' if m.get('role')=='user' else 'BOT')}:\n{(m.get('content') or '')[:1500]}"
        for m in chat_history if isinstance(m, dict)
    )

    # Build user-prompt with context
    pending_list_text = '\n'.join(
        f"  - id={it['id']} | datum={it['datum']} | marker={it['marker']}"
        for it in (ctx.get('pending_review_items') or [])
    )
    groups_text = '\n'.join(
        f"  - {g['group_id']}: {g['date_range']} ({g['label']}, {g['count']} Tage)"
        for g in (ctx.get('review_groups') or [])
    )

    user_prompt = f"""=== JOB-KONTEXT ===
Steuerjahr: {ctx.get('tax_year')}
Aktueller vorläufiger Betrag: {ctx.get('current_total'):.2f} €
Offene Review-Tage: {ctx.get('open_review_count')}
Bereits beantwortet: {ctx.get('answered_review_count')}
PDF-Status: {ctx.get('pdf_status')}

=== AKTIVE GRUPPEN ===
{groups_text or '(keine)'}

=== PENDING REVIEW-ITEMS (id, datum, marker) ===
{pending_list_text or '(keine)'}

=== BISHERIGER CHAT (max 8 letzte) ===
{history_block or '(erste Nachricht)'}

=== NEUE NUTZER-NACHRICHT ===
{user_msg}

Antworte JETZT mit dem strukturierten JSON-Objekt."""

    parsed = None
    ai_used = True
    if ANTHROPIC_KEY:
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=60.0)
            resp = client.messages.create(
                model='claude-sonnet-4-6', max_tokens=1500,
                system=_AI_SYSTEM_PROMPT,
                messages=[{'role': 'user', 'content': user_prompt}],
            )
            text_out = resp.content[0].text.strip()
            # JSON extrahieren
            m = _re.search(r'\{[\s\S]*\}', text_out)
            if m:
                parsed = _json.loads(m.group(0))
        except Exception as e:
            print(f'[ai-chat] Sonnet fail: {e}')
            ai_used = False
    else:
        ai_used = False

    if parsed is None:
        # Fallback: deterministischer Parser
        groups = _build_review_groups(review_items)
        fallback = _interpret_review_text(user_msg, groups, items_by_id)
        if fallback.get('proposed_changes'):
            parsed = {
                'intent': fallback.get('intent', 'review_answer'),
                'message_to_user': (fallback.get('summary_header') or 'Ich habe verstanden:')
                    + ('\n' + '\n'.join(fallback.get('summary_lines') or []) if fallback.get('summary_lines') else ''),
                'needs_confirmation': True,
                'proposed_changes': fallback['proposed_changes'],
                'clarification_question': None,
                'next_action': 'ask_confirmation',
                'referenced_groups': [],
            }
        elif fallback.get('clarification'):
            parsed = {
                'intent': 'clarification',
                'message_to_user': fallback['clarification'],
                'needs_confirmation': False,
                'proposed_changes': [],
                'clarification_question': fallback['clarification'],
                'next_action': 'ask_clarification',
                'referenced_groups': [],
            }
        else:
            parsed = {
                'intent': 'clarification',
                'message_to_user': 'Magst du das nochmal kurz präzisieren? Du kannst auch „alle über 8h", „alle unter 8h" oder einen konkreten Tag wie „07.04 ja" antworten.',
                'needs_confirmation': False,
                'proposed_changes': [],
                'clarification_question': 'Magst du es kurz anders schreiben?',
                'next_action': 'ask_clarification',
                'referenced_groups': [],
            }

    # Validate
    sanitized_changes = []
    for ch in (parsed.get('proposed_changes') or []):
        if not isinstance(ch, dict): continue
        iid = ch.get('review_item_id')
        ans = ch.get('answer')
        if iid not in items_by_id: continue  # AI darf keine fremden IDs
        if items_by_id[iid].get('status') != 'pending': continue
        if ans not in ('yes', 'no', 'unsure'): continue
        if any(c['review_item_id'] == iid for c in sanitized_changes): continue
        sanitized_changes.append({'review_item_id': iid, 'answer': ans})
    parsed['proposed_changes'] = sanitized_changes

    # Bulk MUSS confirmation_required
    if len(sanitized_changes) >= 2:
        parsed['needs_confirmation'] = True

    # estimated_delta + confirmation_id
    estimated_delta = None
    if sanitized_changes and cached.get('matched_days'):
        try:
            preview_overrides = dict(existing_overrides)
            for ch in sanitized_changes:
                it = items_by_id.get(ch['review_item_id'])
                if not it: continue
                d = it.get('datum')
                if not d: continue
                if ch['answer'] == 'yes':
                    preview_overrides[d] = {'over_8h': True, 'source': 'user_ai_interpreted_text'}
                elif ch['answer'] == 'no':
                    preview_overrides[d] = {'over_8h': False, 'source': 'user_ai_interpreted_text'}
                else:
                    preview_overrides[d] = {'unsure': True, 'source': 'user_ai_interpreted_text'}
            rec = _recompute_with_overrides(cached, preview_overrides)
            if rec and rec.get('totals'):
                new_total = float(rec['totals'].get('netto') or 0)
                estimated_delta = round(new_total - initial_total, 2)
        except Exception as e:
            print(f'[ai-chat] preview fail: {e}')

    cid_src = f"{job_id}|{user_msg}|{sorted([(c['review_item_id'], c['answer']) for c in sanitized_changes])}"
    parsed['confirmation_id'] = hashlib.sha256(cid_src.encode('utf-8')).hexdigest()[:16]
    parsed['estimated_delta'] = estimated_delta
    parsed['ai_used'] = ai_used
    parsed['applied'] = False

    return jsonify(parsed)


@app.route('/api/job/<job_id>/review-interpret', methods=['POST'])
def post_review_interpret(job_id):
    """v8.26: Interpretiert Freitext-Antwort und liefert proposed_changes — OHNE anzuwenden.

    Body: {message: str}
    Returns: {intent, proposed_changes, confirmation_required, summary_lines, summary_header,
              confirmation_id, estimated_delta, clarification?}
    """
    body = request.get_json(silent=True) or {}
    message = (body.get('message') or '').strip()
    if not message:
        return jsonify({'error': 'message erforderlich'}), 400

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        review_items = (j.get('data') or {}).get('_review_items') or []
        existing_overrides = dict(j.get('manual_day_overrides') or {})
        cached = (j.get('data') or {}).get('_cached_recalc_state') or {}
        initial_total = float((j.get('data') or {}).get('netto') or 0)

    items_by_id = {it['id']: it for it in review_items}
    groups = _build_review_groups(review_items)
    interp = _interpret_review_text(message, groups, items_by_id)

    estimated_delta = None
    if interp['proposed_changes'] and cached.get('matched_days'):
        try:
            preview_overrides = dict(existing_overrides)
            for ch in interp['proposed_changes']:
                it = items_by_id.get(ch['review_item_id'])
                if not it: continue
                d = it.get('datum')
                if not d: continue
                if ch['answer'] == 'yes':
                    preview_overrides[d] = {'over_8h': True, 'source': 'user_bulk_review_chatbot_text'}
                elif ch['answer'] == 'no':
                    preview_overrides[d] = {'over_8h': False, 'source': 'user_bulk_review_chatbot_text'}
                else:
                    preview_overrides[d] = {'unsure': True, 'source': 'user_bulk_review_chatbot_text'}
            rec = _recompute_with_overrides(cached, preview_overrides)
            if rec and rec.get('totals'):
                new_total = float(rec['totals'].get('netto') or 0)
                estimated_delta = round(new_total - initial_total, 2)
        except Exception as _e:
            print(f'[review-interpret] preview fail: {_e}')

    # Confirmation-ID: deterministisch aus Inhalt + jobid
    import hashlib
    cid_src = job_id + '|' + message + '|' + str(sorted([(c['review_item_id'], c['answer']) for c in interp['proposed_changes']]))
    confirmation_id = hashlib.sha256(cid_src.encode('utf-8')).hexdigest()[:16]

    return jsonify({
        'intent':                interp['intent'],
        'proposed_changes':      interp['proposed_changes'],
        'confirmation_required': interp['confirmation_required'],
        'summary_lines':         interp.get('summary_lines') or [],
        'summary_header':        interp.get('summary_header') or 'Ich habe verstanden:',
        'clarification':         interp.get('clarification'),
        'confirmation_id':       confirmation_id,
        'estimated_delta':       estimated_delta,
        'applied':               False,
    })


@app.route('/api/job/<job_id>/review-answer-bulk', methods=['POST'])
def post_review_answer_bulk(job_id):
    """v8.26: Wendet bestätigte Bulk-Antworten an. Verlangt confirmation_id.

    Body: {confirmation_id, proposed_changes: [{review_item_id, answer}], source?}
    """
    body = request.get_json(silent=True) or {}
    confirmation_id = (body.get('confirmation_id') or '').strip()
    proposed = body.get('proposed_changes') or []
    source = (body.get('source') or 'user_bulk_review_chatbot_text').strip()

    if not confirmation_id:
        return jsonify({'error': 'confirmation_id erforderlich — bitte über review-interpret bestätigen'}), 400
    if not proposed or not isinstance(proposed, list):
        return jsonify({'error': 'proposed_changes erforderlich'}), 400

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        review_items = (j.get('data') or {}).get('_review_items') or []
        items_by_id = {it['id']: it for it in review_items}
        existing_overrides = dict(j.get('manual_day_overrides') or {})

        # Confirmation-ID neu berechnen + abgleichen (Anti-Replay/Tampering)
        import hashlib
        # Wir können die ursprüngliche message hier nicht reproduzieren — vertrauen auf Client
        # CID ist primär Marker dafür, dass User explizit bestätigt hat.

        applied = []
        for ch in proposed:
            iid = ch.get('review_item_id')
            ans = ch.get('answer')
            if not iid or ans not in ('yes', 'no', 'unsure'): continue
            it = items_by_id.get(iid)
            if not it: continue
            d = it.get('datum')
            if not d: continue
            if it.get('status') == 'answered': continue
            if ans == 'yes':
                existing_overrides[d] = {'over_8h': True, 'source': source}
            elif ans == 'no':
                existing_overrides[d] = {'over_8h': False, 'source': source}
            else:
                existing_overrides[d] = {'unsure': True, 'source': source}
            applied.append({'datum': d, 'answer': ans, 'review_item_id': iid})
            it['status'] = 'answered'
            it['user_answer'] = existing_overrides[d]

        j['manual_day_overrides'] = existing_overrides
        if 'audit' in j and isinstance(j['audit'], list):
            j['audit'].append({
                'event': 'review_answer_bulk',
                'data': {'confirmation_id': confirmation_id, 'applied_count': len(applied),
                         'source': source, 'applied': applied},
                'timestamp': datetime.now().isoformat(),
            })

        # Recompute
        cached = (j.get('data') or {}).get('_cached_recalc_state') or {}
        updated_preview_total = None
        total_delta = None
        recalc_breakdown = None
        if cached.get('matched_days'):
            try:
                rec = _recompute_with_overrides(cached, existing_overrides)
                if rec and rec.get('totals'):
                    updated_preview_total = rec['totals'].get('netto')
                    initial_total = float((j.get('data') or {}).get('netto') or 0)
                    if isinstance(updated_preview_total, (int, float)):
                        total_delta = round(float(updated_preview_total) - initial_total, 2)
                    recalc_breakdown = rec['totals']
                    j.setdefault('data', {})['_preview_totals'] = recalc_breakdown
            except Exception as _e:
                print(f'[review-bulk-text] recalc fail: {_e}')

        try:
            _save_job_to_disk(job_id)
        except Exception as _e:
            print(f'[review-bulk-text] save warning: {_e}')

    # v8.37: Session.result_data._review_items mit-updaten, damit Recall den
    # post-review-Stand sieht (nicht stale 22 pending). Suche Session(s) mit gleichem job_id.
    try:
        _sync_session_result_with_job(job_id, j)
    except Exception as _e:
        print(f'[review-bulk-text] session sync warn: {_e}')

    return jsonify({
        'applied_count':         len(applied),
        'applied':               applied,
        'updated_preview_total': updated_preview_total,
        'total_delta':           total_delta,
        'preview_breakdown':     recalc_breakdown,
        'source':                source,
    })


@app.route('/api/job/<job_id>/marker-answer', methods=['POST'])
def post_marker_answer(job_id):
    """v8.22 Rest-5: Nutzer erklärt eine unbekannte Kennung.

    Body: {first_token, meaning, activity_type, datum, raw_marker, airline?, doc_type?}

    Speichert User-Antwort als learning_candidate (file-based marker_lexicon.json).
    Bei 3+ konsistenten Bestätigungen → status='approved'.
    HINWEIS: Das Lexikon wird gepflegt, der Klassifikator nutzt approved-Marker
    aktuell noch NICHT automatisch beim nächsten Job — Integration folgt in
    einer späteren Version. User-facing keine Zukunftsversprechen.
    """
    body = request.get_json(silent=True) or {}
    first_token = (body.get('first_token') or '').strip().upper()
    meaning = (body.get('meaning') or '').strip()
    activity_type = (body.get('activity_type') or '').strip().lower()
    datum = (body.get('datum') or '').strip()
    raw_marker = (body.get('raw_marker') or '').strip()
    airline = (body.get('airline') or 'LH').strip().upper()
    doc_type = (body.get('doc_type') or 'flugstundenuebersicht').strip().lower()

    if not first_token or not meaning:
        return jsonify({'error': 'first_token und meaning erforderlich'}), 400
    if activity_type not in ('flight', 'training', 'sim', 'office', 'standby',
                              'free', 'other', '', 'tour', 'same_day', 'unknown'):
        return jsonify({'error': f'unbekannter activity_type: {activity_type}'}), 400

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404

    result = _record_marker_learning(
        airline=airline, doc_type=doc_type,
        first_token=first_token, meaning=meaning,
        activity_type=activity_type, job_id=job_id,
        datum=datum, raw_marker=raw_marker,
    )

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if j and 'audit' in j and isinstance(j['audit'], list):
            j['audit'].append({
                'event': 'marker_answer',
                'data': {'first_token': first_token, 'meaning': meaning,
                         'activity_type': activity_type, 'datum': datum,
                         'lexicon_status': result.get('status') if result else 'unknown'},
                'timestamp': datetime.now().isoformat(),
            })
            try:
                _save_job_to_disk(job_id)
            except Exception:
                pass

    return jsonify({
        'first_token': first_token,
        'meaning': meaning,
        'activity_type': activity_type,
        'lexicon_status': result.get('status') if result else 'unknown',
        'confirmed_count': result.get('confirmed_count', 0) if result else 0,
        # v8.23: ehrliches User-Wording — kein Zukunftsversprechen
        'user_message': 'Für diese Auswertung berücksichtigt. Als Lernkandidat gespeichert.',
    })


@app.route('/api/job/<job_id>/upload-replacement', methods=['POST'])
def post_upload_replacement(job_id):
    """v8.22 Now-5 (Stub): Endpoint für Document-Replacement im Chat.

    Body multipart/form-data: file, doc_type ('lsb'|'se'|'dp'|'einsatz'|'opt')

    Aktuell als Stub implementiert: nimmt Datei entgegen, validiert Format,
    persistiert temp + bestätigt Upload. Vollständige selektive Re-Read-Pipeline
    folgt in v8.23 (siehe TODO).
    """
    if 'file' not in request.files:
        return jsonify({'error': 'Keine Datei im Upload-Body'}), 400
    file = request.files['file']
    doc_type = (request.form.get('doc_type') or '').strip().lower()
    if doc_type not in ('lsb', 'se', 'dp', 'einsatz', 'other'):
        return jsonify({'error': f'unbekannter doc_type: {doc_type}'}), 400
    if not file or not file.filename:
        return jsonify({'error': 'leere Datei'}), 400
    fname = file.filename
    fext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if fext not in ('pdf', 'jpg', 'jpeg', 'png', 'heic', 'heif'):
        return jsonify({'error': f'Format nicht unterstützt: .{fext}'}), 400

    file_size = 0
    try:
        file.seek(0, 2)
        file_size = file.tell()
        file.seek(0)
    except Exception:
        pass

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        # v8.23: pending_reread-Flag blockiert finalize-pdf bis volle Re-Verarbeitung
        # implementiert ist. Ehrlich begrenzt — keine UI-Behauptung "aktualisiert".
        j['pending_reread'] = True
        j['pending_reread_doc_types'] = list(set(
            (j.get('pending_reread_doc_types') or []) + [doc_type]
        ))
        j['pending_reread_at'] = datetime.now().isoformat()
        if 'audit' in j and isinstance(j['audit'], list):
            j['audit'].append({
                'event': 'document_replacement_received_pending_reread',
                'data': {
                    'doc_type': doc_type, 'filename': fname,
                    'size_bytes': file_size,
                    'status': 'received_pending_reread',
                    'source': 'user_uploaded_replacement',
                    'note': 'Selektives Re-Read pro Doc-Typ noch nicht implementiert. '
                            'Datei vorgemerkt, finalize-pdf blockiert bis Re-Verarbeitung.',
                },
                'timestamp': datetime.now().isoformat(),
            })
        try:
            _save_job_to_disk(job_id)
        except Exception:
            pass

    return jsonify({
        'status': 'received_pending_reread',
        'pending_reread': True,
        'doc_type': doc_type,
        'filename': fname,
        'message': 'Datei erhalten. Die erneute Auswertung ist noch nicht abgeschlossen. '
                   'Du brauchst eine vollständige Neu-Auswertung, damit der ersetzte Beleg '
                   'in deine Werte einfließt.',
        'next_action': 'restart_evaluation',
        'pdf_blocked': True,
    }), 202


def _validate_cas_reader_output(parsed):
    """v10: Validiert Sonnet-Output gegen das Targeted-Reader-Schema.

    Erwartet entweder das v2-Format mit 'matches' ODER das alte 'days'-Format
    (Backwards-Compat während Migration). Returns (matches_list, errors_list).
    """
    matches = []
    errors = []
    if not isinstance(parsed, dict):
        return matches, ['root_not_dict']
    # v2-Format
    if 'matches' in parsed and isinstance(parsed['matches'], list):
        for entry in parsed['matches']:
            if not isinstance(entry, dict):
                errors.append('match_not_dict')
                continue
            rid = entry.get('review_item_id')
            datum = entry.get('date') or entry.get('datum')
            status = entry.get('status', '').lower()
            if status not in ('found', 'not_found'):
                errors.append(f'invalid_status:{status}')
                continue
            if status == 'found':
                st = entry.get('start_time', '')
                et = entry.get('end_time', '')
                if not (st and et):
                    # status=found ohne Zeit → downgrade auf not_found
                    matches.append({'review_item_id': rid, 'date': datum, 'status': 'not_found',
                                    'confidence': 'low'})
                    continue
            matches.append({
                'review_item_id': rid,
                'date': datum,
                'status': status,
                'marker': entry.get('marker', ''),
                'start_time': entry.get('start_time', '') if status == 'found' else '',
                'end_time': entry.get('end_time', '') if status == 'found' else '',
                'confidence': (entry.get('confidence', 'medium') or 'medium').lower(),
                'raw_excerpt': str(entry.get('raw_excerpt', ''))[:200],
            })
        return matches, errors
    # Legacy v1: 'days'-Liste — als found-matches behandeln (review_item_id wird später gemapped)
    if 'days' in parsed and isinstance(parsed['days'], list):
        for d in parsed['days']:
            if not isinstance(d, dict): continue
            datum = d.get('datum') or d.get('date')
            if not datum: continue
            matches.append({
                'review_item_id': None,
                'date': datum,
                'status': 'found' if (d.get('start_time') and d.get('end_time')) else 'not_found',
                'marker': d.get('marker', ''),
                'start_time': d.get('start_time', ''),
                'end_time': d.get('end_time', ''),
                'confidence': 'medium',
                'raw_excerpt': '',
            })
        return matches, errors
    return matches, ['no_matches_or_days_field']


@app.route('/api/job/<job_id>/upload-roster-screenshot', methods=['POST'])
def post_upload_roster_screenshot(job_id):
    """v10 Targeted CAS Reader: Sonnet Vision sucht ausschließlich nach Ziel-Tagen
    aus den pending review_items und liefert striktes Schema mit Status, Confidence
    und Roh-Auszug. KEIN Auto-Apply: User bestätigt im Chat.

    Cross-File-Conflicts werden über job-state tracking detektiert. Duplikate (gleiche
    Datei doppelt) via SHA-256 deduped.

    Body multipart/form-data: file (PDF/JPG/PNG/HEIC ≤8MB)
    Returns: {matches[], conflicts[], recognized_count, matched_count, proposed_changes,
              detected_days, unmatched_dates, confirmation_id, applied: False,
              source_file_id, source_filename, source_hint}
    """
    import base64, json, re, hashlib

    if 'file' not in request.files:
        return jsonify({'error': 'Keine Datei im Upload-Body'}), 400
    file = request.files['file']
    fname = file.filename or 'screenshot'
    fext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
    if fext not in ('pdf', 'jpg', 'jpeg', 'png', 'heic', 'heif'):
        return jsonify({'error': f'Format nicht unterstützt: .{fext}'}), 400
    if not ANTHROPIC_KEY:
        return jsonify({'error': 'Auswertung gerade nicht verfügbar — bitte später erneut.'}), 503

    file_bytes = file.read()
    if not file_bytes or len(file_bytes) > 8 * 1024 * 1024:
        return jsonify({'error': 'Datei leer oder zu groß (max 8 MB)'}), 400

    # v10: SHA-256 als file_id für Dedupe (gleiche Datei doppelt = ignorieren)
    file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]

    # Job laden
    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        review_items = (j.get('data') or {}).get('_review_items') or []
        existing_overrides = dict(j.get('manual_day_overrides') or {})
        cached = (j.get('data') or {}).get('_cached_recalc_state') or {}
        initial_total = float((j.get('data') or {}).get('netto') or 0)
        # v10: Cross-File-State für Conflict-Detection
        cas_detected_per_date = dict(j.get('_cas_detected_per_date') or {})
        seen_file_hashes = set(j.get('_cas_seen_file_hashes') or [])

    # v10: Dedupe — gleiche Datei doppelt → freundlich melden
    if file_hash in seen_file_hashes:
        return jsonify({
            'matches': [],
            'conflicts': [],
            'recognized_count': 0,
            'matched_count': 0,
            'pending_total': len([it for it in review_items if it.get('status') == 'pending']),
            'detected_days': [],
            'proposed_changes': [],
            'unmatched_dates': [],
            'confirmation_id': '',
            'applied': False,
            'source_file_id': file_hash,
            'source_filename': fname,
            'source_hint': 'user_uploaded_roster_cas_detected',
            'duplicate_file_skipped': True,
            'message': 'Diese Datei wurde bereits ausgewertet — überspringe sie.',
        })

    pending = [it for it in review_items if it.get('status') == 'pending']
    if not pending:
        return jsonify({'error': 'Keine offenen Tage — Datei wird nicht benötigt'}), 400

    # v10: Target-Liste mit review_item_id + Marker-Hint für stärkere Sonnet-Lenkung
    targets = []
    for it in pending:
        targets.append({
            'review_item_id': it.get('id', ''),
            'date': it.get('datum', ''),
            'marker_hint': it.get('marker', ''),
            'activity': it.get('activity_type', ''),
        })
    pending_dates = [t['date'] for t in targets if t['date']]

    # Sonnet Vision Call
    media_type_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                      'png': 'image/png', 'heic': 'image/heic',
                      'heif': 'image/heif', 'pdf': 'application/pdf'}
    media_type = media_type_map.get(fext, 'image/jpeg')
    b64 = base64.standard_b64encode(file_bytes).decode('utf-8')

    # v10: Strikter Target-Reader-Prompt. Sucht ausschließlich Ziel-Tage,
    # erfindet keine Zeiten, gibt status='not_found' bei Unsicherheit.
    target_lines = []
    for t in targets[:60]:  # cap auf 60 Targets pro Call (Token-Budget)
        target_lines.append(f"  - {t['date']}  (review_item_id={t['review_item_id']}, marker={t['marker_hint'] or 'n/a'})")
    prompt = (
        "Du liest einen Dienstplan/CAS/Roster für eine Lufthansa-Crewmember.\n\n"
        "Wichtig:\n"
        "- Suche ausschließlich nach den unten genannten Ziel-Tagen.\n"
        "- Ignoriere alle anderen Tage im Dienstplan.\n"
        "- Erfinde keine Zeiten. Wenn ein Ziel-Tag nicht sicher erkennbar ist, gib status='not_found' zurück.\n"
        "- Gib keine steuerliche Bewertung ab.\n\n"
        "Ziel-Tage:\n"
        + '\n'.join(target_lines) + '\n\n'
        "Extrahiere pro Ziel-Tag ein Match-Objekt mit diesen Feldern:\n"
        "  - review_item_id: exakt der Ziel-ID aus der Liste\n"
        "  - date: YYYY-MM-DD\n"
        "  - status: 'found' wenn Datum+Zeiten sicher erkennbar, sonst 'not_found'\n"
        "  - marker: Terminname/Kürzel aus dem Plan (z.B. 'EK BUERODIENST', 'D4 SCHULUNG')\n"
        "  - start_time: HH:MM (24h), nur bei status='found'\n"
        "  - end_time: HH:MM (24h), nur bei status='found'\n"
        "  - confidence: 'high' wenn klar lesbar, 'medium' wenn etwas unsicher, 'low' wenn sehr unsicher\n"
        "  - raw_excerpt: kurzer sichtbarer Zeilenausschnitt (max 100 Zeichen)\n\n"
        "Antwort ausschließlich als JSON in diesem Schema:\n"
        '{"matches": [{"review_item_id": "...", "date": "YYYY-MM-DD", "status": "found", "marker": "...", "start_time": "HH:MM", "end_time": "HH:MM", "confidence": "high", "raw_excerpt": "..."}, ...]}\n'
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=60.0)
        if fext == 'pdf':
            content = [
                {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf', 'data': b64}},
                {'type': 'text', 'text': prompt},
            ]
        else:
            content = [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': media_type, 'data': b64}},
                {'type': 'text', 'text': prompt},
            ]
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=3000,
            messages=[{'role': 'user', 'content': content}],
        )
        text_out = resp.content[0].text
    except Exception as e:
        print(f'[upload-roster-screenshot] Sonnet fail: {e}')
        return jsonify({'error': 'Konnte die Datei gerade nicht auswerten — bitte mit klarerem Screenshot erneut versuchen.'}), 502

    # v10: JSON parsen + gegen Schema validieren
    m = re.search(r'\{[\s\S]*\}', text_out or '')
    matches_raw = []
    schema_errors = []
    if m:
        try:
            parsed = json.loads(m.group(0))
            matches_raw, schema_errors = _validate_cas_reader_output(parsed)
        except Exception as e:
            print(f'[upload-roster-screenshot] JSON parse fail: {e}')
            schema_errors.append('json_parse_fail')

    # v10: Map auf review_items (Review-IDs in Target-Liste sind Wahrheit, Datum als Fallback)
    items_by_id = {it.get('id'): it for it in review_items}
    pending_by_date = {it.get('datum'): it for it in pending}
    valid_target_ids = {t['review_item_id'] for t in targets}

    matches = []
    proposed_changes = []
    detected_days = []  # backwards-compat
    matched_dates = set()
    conflicts = []

    for m_entry in matches_raw:
        rid = m_entry.get('review_item_id')
        datum = m_entry.get('date')
        # Sicherheit: review_item_id MUSS in Target-Liste sein (kein fremder Tag)
        target_item = None
        if rid and rid in valid_target_ids:
            target_item = items_by_id.get(rid)
        elif datum and datum in pending_by_date:
            target_item = pending_by_date[datum]
            rid = target_item.get('id')
        if not target_item:
            # Fremder Tag oder ungültige ID → silently skip (Reader hat Anweisung übertreten)
            continue
        # Datum-Konsistenz prüfen
        if datum and datum != target_item.get('datum'):
            continue  # Reader hat ID falsch zugeordnet → skip
        datum = target_item.get('datum')

        status = m_entry.get('status', 'not_found')
        st = m_entry.get('start_time', '') or ''
        et = m_entry.get('end_time', '') or ''

        match_obj = {
            'review_item_id': rid,
            'date': datum,
            'status': status,
            'marker': m_entry.get('marker', ''),
            'start_time': st,
            'end_time': et,
            'duration_minutes': None,
            'confidence': m_entry.get('confidence', 'medium'),
            'source_file_id': file_hash,
            'source_filename': fname,
            'raw_excerpt': m_entry.get('raw_excerpt', ''),
        }

        if status == 'found' and st and et:
            try:
                sh, sm = map(int, st.split(':')[:2])
                eh, em = map(int, et.split(':')[:2])
                minutes = (eh * 60 + em) - (sh * 60 + sm)
                if minutes < 0:
                    minutes += 24 * 60
                match_obj['duration_minutes'] = minutes
            except Exception:
                # Ungültige Zeit → downgrade auf not_found
                match_obj['status'] = 'not_found'
                match_obj['start_time'] = ''
                match_obj['end_time'] = ''
                matches.append(match_obj)
                continue

            # v10: Cross-File-Conflict-Detection
            prior = cas_detected_per_date.get(datum) or []
            time_sig = f"{st}-{et}"
            existing_sigs = [(p.get('start_time', '') + '-' + p.get('end_time', '')) for p in prior]
            if existing_sigs and time_sig not in existing_sigs:
                # Conflict — andere Zeit für selben Tag aus früherer Datei
                conflicts.append({
                    'review_item_id': rid,
                    'date': datum,
                    'candidates': prior + [{'start_time': st, 'end_time': et,
                                             'source_filename': fname,
                                             'source_file_id': file_hash}],
                })
                matches.append(match_obj)
                # Conflict → KEIN proposed_change (User muss wählen)
                continue
            if time_sig in existing_sigs:
                # Dedupe — selbe Zeit, neuer File → bestätigt vorhanden, kein neuer change
                matches.append(match_obj)
                matched_dates.add(datum)
                continue

            # Neue Erkennung → Track + proposed_change
            cas_detected_per_date.setdefault(datum, []).append({
                'start_time': st, 'end_time': et,
                'source_filename': fname, 'source_file_id': file_hash,
            })
            over_8h = minutes > 480
            proposed_changes.append({
                'review_item_id': rid,
                'answer': 'yes' if over_8h else 'no',
                'detected_start': st,
                'detected_end': et,
                'detected_minutes': minutes,
                'source_file_id': file_hash,
                'source_filename': fname,
                'confidence': match_obj['confidence'],
            })
            detected_days.append({'datum': datum, 'start_time': st, 'end_time': et})
            matched_dates.add(datum)

        matches.append(match_obj)

    # Confirmation-ID (deterministisch über sortierte review_item_ids + Zeiten)
    cid_payload = sorted([(c.get('review_item_id', ''), c.get('detected_start', ''),
                           c.get('detected_end', '')) for c in proposed_changes])
    cid_src = job_id + '|cas_v10|' + json.dumps(cid_payload)
    confirmation_id = hashlib.sha256(cid_src.encode('utf-8')).hexdigest()[:16]

    # Audit-Eintrag + Cross-File-State persistieren
    with _jobs_lock:
        j2 = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if j2:
            j2['_cas_detected_per_date'] = cas_detected_per_date
            seen = set(j2.get('_cas_seen_file_hashes') or [])
            seen.add(file_hash)
            j2['_cas_seen_file_hashes'] = sorted(seen)
            if 'audit' in j2 and isinstance(j2['audit'], list):
                j2['audit'].append({
                    'event': 'roster_cas_uploaded',
                    'data': {
                        'filename': fname,
                        'source_file_id': file_hash,
                        'recognized_count': sum(1 for m in matches if m.get('status') == 'found'),
                        'matched_count': len(proposed_changes),
                        'conflicts_count': len(conflicts),
                        'pending_total': len(pending_dates),
                        'confirmation_id': confirmation_id,
                        'schema_errors': schema_errors,
                        'source': 'user_uploaded_roster_cas_detected',
                    },
                    'timestamp': datetime.now().isoformat(),
                })
            try: _save_job_to_disk(job_id)
            except Exception: pass

    return jsonify({
        # v10 neue Felder
        'matches':           matches,
        'conflicts':         conflicts,
        'source_file_id':    file_hash,
        'source_filename':   fname,
        # Backwards-compat Felder (Frontend nutzt diese im Multi-CAS-Flow)
        'recognized_count':  sum(1 for m in matches if m.get('status') == 'found'),
        'matched_count':     len(proposed_changes),
        'pending_total':     len(pending_dates),
        'detected_days':     detected_days,
        'proposed_changes':  proposed_changes,
        'unmatched_dates':   [d for d in pending_dates if d not in matched_dates],
        'confirmation_id':   confirmation_id,
        'applied':           False,
        'source_hint':       'user_uploaded_roster_cas_detected',
    })


@app.route('/api/job/<job_id>/finalize-pdf', methods=['POST'])
def post_finalize_pdf(job_id):
    """v8.22 Step E: Erstellt finales PDF unter Berücksichtigung aller User-Review-
    Antworten (manual_day_overrides). Re-klassifiziert deterministisch, ruft
    erstelle_pdf mit aktualisiertem Result-Dict, persistiert das neue PDF und
    liefert finale Werte zurück.

    Body (optional): {skip_unanswered: bool}  — bei True werden offene Items
    als "nicht bestätigt" im Audit markiert und das PDF trotzdem erstellt.
    """
    body = request.get_json(silent=True) or {}
    skip_unanswered = bool(body.get('skip_unanswered', False))

    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if not j:
            return jsonify({'error': 'Diese Auswertung ist nicht mehr verfügbar — bitte starte eine neue Auswertung.'}), 404
        # Job-Status muss done sein (Berechnung abgeschlossen)
        if j.get('status') != 'done':
            return jsonify({'error': 'Auswertung noch nicht abgeschlossen'}), 400
        # v8.23: PDF blockiert wenn ein Dokument zur Re-Verarbeitung vorgemerkt ist
        if j.get('pending_reread'):
            return jsonify({
                'error': 'Eine Datei wartet auf erneute Auswertung — bitte erst Auswertung neu starten.',
                'pending_reread': True,
                'pending_reread_doc_types': j.get('pending_reread_doc_types', []),
            }), 409
        data = dict(j.get('data') or {})
        cached = data.get('_cached_recalc_state') or {}
        overrides = j.get('manual_day_overrides') or {}
        # v9.2 Hard-Gate: offene Review-Items + nicht skip → 409
        if not skip_unanswered:
            review_items = data.get('_review_items') or []
            still_pending = [it for it in review_items if it.get('status') == 'pending']
            if still_pending:
                return jsonify({
                    'error': f'Es sind noch {len(still_pending)} Tage ungeklärt. '
                              f'Beantworte sie im Chat — oder rufe den Endpoint mit '
                              f'skip_unanswered=true auf, dann werden sie als nicht bestätigt notiert.',
                    'pending_review_count': len(still_pending),
                }), 409

    if not cached or not cached.get('matched_days'):
        return jsonify({
            'error': 'Recalc-State nicht verfügbar — bitte Auswertung erneut starten.',
        }), 409

    # Re-Compute mit aktuellen Overrides (deterministisch, kein Sonnet-Call)
    try:
        rec = _recompute_with_overrides(cached, overrides)
        if not rec:
            return jsonify({'error': 'Re-Berechnung fehlgeschlagen.'}), 500
    except Exception as e:
        print(f'[finalize-pdf] recalc fail: {e}')
        return jsonify({'error': 'Re-Berechnung fehlgeschlagen.', 'detail': str(e)[:120]}), 500

    # Result-Dict mit neuen Totals patchen (PDF-Generator nutzt die Felder direkt)
    final_data = dict(data)
    final_data.update(rec['totals'])
    # Audit-Notes für skipped/answered ergänzen
    notes_existing = list(final_data.get('notes') or [])
    answered = sum(1 for v in overrides.values()
                   if isinstance(v, dict) and not v.get('unsure'))
    unsure_n = sum(1 for v in overrides.values()
                   if isinstance(v, dict) and v.get('unsure'))
    # v10: CAS-Quelle und Skip-Hinweis transparent im Audit dokumentieren.
    cas_detected_dates = j.get('_cas_detected_per_date') or {}
    cas_used_count = 0
    if cas_detected_dates:
        # Wie viele der angewendeten Antworten kamen aus CAS-Quelle?
        for k, v in overrides.items():
            if isinstance(v, dict) and v.get('source') in (
                'user_uploaded_roster_cas_detected',
                'user_uploaded_roster_screenshot',
                'user_uploaded_roster_multi_confirmed',
            ):
                cas_used_count += 1
    if cas_used_count > 0:
        notes_existing.append(
            f'ℹ Für {cas_used_count} Tag(e) wurden Zeiten aus einem optional '
            f'hochgeladenen Dienstplan/CAS erkannt und vom Nutzer bestätigt.'
        )
    if answered > 0:
        notes_existing.append(
            f'ℹ {answered} Schulungs-/Office-Tag(e) durch deine Antworten ergänzt.'
        )
    if unsure_n > 0:
        notes_existing.append(
            f'ℹ {unsure_n} Tag(e) nicht bestätigt (User unsicher) — im Nachweis vermerkt.'
        )
    if skip_unanswered:
        notes_existing.append(
            'ℹ Nicht bestätigte Punkte wurden nicht zusätzlich berücksichtigt.'
        )
    final_data['notes'] = notes_existing

    # PDF generieren
    try:
        pdf_bytes = erstelle_pdf(final_data).getvalue()
    except Exception as e:
        print(f'[finalize-pdf] erstelle_pdf fail: {e}')
        return jsonify({'error': 'PDF-Erstellung fehlgeschlagen.', 'detail': str(e)[:120]}), 500

    # PDF unter Token speichern (überschreibt das alte)
    download_url = data.get('download_url') or ''
    token = ''
    if download_url and '/api/download/' in download_url:
        token = download_url.rsplit('/', 1)[-1]
    if not token:
        # Neues Token wenn nicht vorhanden
        import secrets as _s
        token = _s.token_urlsafe(16)
        download_url = f'/api/download/{token}'

    name_safe = (final_data.get('name') or 'Auswertung').replace(' ', '_')
    year_safe = final_data.get('year', 2025)
    filename = f'AeroTAX_Auswertung_{year_safe}_{name_safe}.pdf'
    try:
        _save_pdf(token, pdf_bytes, filename)
    except Exception as e:
        print(f'[finalize-pdf] save_pdf fail: {e}')
        return jsonify({'error': 'PDF-Speicherung fehlgeschlagen.'}), 500

    # Job-State aktualisieren
    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
        if j:
            j['data'] = final_data
            j['data']['download_url'] = download_url
            j['download_url'] = download_url
            j['pdf_status'] = 'ready'
            j['pdf_finalized_at'] = datetime.now().isoformat()
            if 'audit' in j and isinstance(j['audit'], list):
                j['audit'].append({
                    'event': 'pdf_finalized',
                    'data': {
                        'overrides_applied': len(overrides),
                        'answered': answered, 'unsure': unsure_n,
                        'final_total': final_data.get('netto'),
                    },
                    'timestamp': datetime.now().isoformat(),
                })
            try:
                _save_job_to_disk(job_id)
            except Exception as _e:
                print(f'[finalize-pdf] save warning: {_e}')

    return jsonify({
        'final_total':  final_data.get('netto'),
        'pdf_status':   'ready',
        'download_url': download_url,
        'overrides_applied': len(overrides),
        'answered':     answered,
        'unsure':       unsure_n,
    })


@app.route('/api/recover', methods=['POST'])
def recover_failed_job():
    """Vereinfacht: Session-Token reicht für Retry. Max 1 kostenloser Retry — danach Support.
    Body: {token}. Token muss noch gültig sein (24h ab Bezahlung).
    Retry-Counter im In-Memory _recovery_tokens (V1 acceptance).
    """
    body = request.get_json(silent=True) or {}
    token = body.get('token', '').strip()
    if not token:
        return jsonify({'error': 'token erforderlich'}), 400
    session = _load_session(token)
    if not session:
        return jsonify({'error': 'Token ungültig oder abgelaufen (24h ab Bezahlung)'}), 403
    info = _recovery_tokens.get(token, {})
    if int(info.get('retries_used', 0)) >= 1:
        return jsonify({
            'error': 'Du hast bereits einen kostenlosen Retry genutzt. Bitte kontaktiere Support — wir helfen dir persönlich.',
            'support': True,
        }), 403
    _recovery_tokens[token] = {
        'token': token,
        'retries_used': int(info.get('retries_used', 0)) + 1,
        'expires': (datetime.utcnow() + timedelta(minutes=60)).isoformat() + 'Z',
    }
    return jsonify({
        'ok': True,
        'message': 'Du kannst innerhalb der nächsten 60 Min die Dokumente erneut hochladen — ohne erneute Bezahlung. Bei einem weiteren Fehler wende dich bitte an den Support.',
        'free_retry_token': token,
    })


@app.route('/api/health', methods=['GET'])
def quick_health():
    """v8.40: Schneller Health-Check ohne externe Calls. Frontend nutzt das bei
    Verbindungsfehlern um zu unterscheiden: Server down vs. Endpoint-spezifisch."""
    return jsonify({
        'ok': True,
        'service': 'aerotax-backend',
        'version': 'v8.40',
    })


@app.route('/api/health/full', methods=['GET'])
def full_health_check():
    """End-to-End Health Check: Server, Anthropic API, File-System."""
    health = {'server': 'ok', 'timestamp': datetime.utcnow().isoformat() + 'Z'}
    # Anthropic
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        r = client.messages.create(
            model='claude-haiku-4-5-20251001', max_tokens=10,
            messages=[{'role':'user','content':'pong'}])
        health['anthropic'] = 'ok' if r.content else 'no_content'
    except Exception as e:
        health['anthropic'] = f'fail: {str(e)[:120]}'
    # File system
    try:
        test_path = os.path.join(_JOBS_DIR, '.health-check')
        with open(test_path, 'w') as f: f.write('ok')
        os.remove(test_path)
        health['filesystem'] = 'ok'
    except Exception as e:
        health['filesystem'] = f'fail: {str(e)[:120]}'
    # PIL/HEIF
    health['pil'] = 'ok' if PIL_AVAILABLE else 'missing'
    health['heif'] = 'ok' if HEIF_AVAILABLE else 'missing'
    # Stripe — verifiziert dass STRIPE_SECRET_KEY konfiguriert + valide ist
    try:
        if not stripe.api_key:
            health['stripe'] = 'missing_key'
        else:
            stripe.Account.retrieve()
            health['stripe'] = 'ok'
    except Exception as e:
        health['stripe'] = f'fail: {str(e)[:120]}'
    # Supabase — DB read + uploaded_files / pdfs Tabellen erreichbar
    if not SB_AVAILABLE:
        health['supabase'] = 'not_configured'
    else:
        try:
            sb.table('sessions').select('token').limit(1).execute()
            sb.table('pdfs').select('token').limit(1).execute()
            sb.table('uploaded_files').select('id').limit(1).execute()
            health['supabase'] = 'ok'
        except Exception as e:
            health['supabase'] = f'fail: {str(e)[:120]}'
    overall = 'ok' if all(v == 'ok' for k, v in health.items() if k not in ('timestamp', 'server', 'heif')) else 'degraded'
    health['overall'] = overall
    return jsonify(health), 200 if overall == 'ok' else 503


# Recovery-Tokens: erlauben kostenlose Wiederholung in 60-Min-Fenster
_recovery_tokens = {}


def _is_valid_recovery_token(token):
    """True, wenn Recovery/Edit-Token existiert und noch nicht abgelaufen ist."""
    info = _recovery_tokens.get(token or '')
    if not info:
        return False
    try:
        exp = datetime.fromisoformat(str(info.get('expires', '')).replace('Z', ''))
        if exp < datetime.utcnow():
            _recovery_tokens.pop(token, None)
            return False
    except Exception:
        _recovery_tokens.pop(token, None)
        return False
    return True


# ══════════════════════════════════════════════════════════════════
#  SESSION TOKENS — Premium-Chat & Result-Recall (24h gültig, Datenschutz-First)
# ══════════════════════════════════════════════════════════════════
# Nach erfolgreicher Auswertung bekommt der User einen Session-Token.
# Damit kann er 24h lang:
#   • Sein Auswertungs-Ergebnis erneut abrufen
#   • Mit AeroTAX über sein konkretes Ergebnis chatten
# Token läuft nach 24h ab — Datenschutz-First, keine Langzeit-Speicherung.

_SESSION_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sessions')
os.makedirs(_SESSION_DIR, exist_ok=True)


SESSION_HOURS = 24  # Session-Token nur 24h gültig — Datenschutz-First

def _make_session_token(job_id):
    """Generiert kurzlebigen Session-Token nach erfolgreicher Auswertung."""
    secret = os.environ.get('SESSION_SECRET', 'aerosteuer-session-default-2025')
    raw = f"{job_id}:{datetime.utcnow().isoformat()}:{secret}"
    return 'AT-' + _hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


_MARKER_LEXICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'marker_lexicon.json')


def _load_marker_lexicon():
    """v8.22 Rest-5: Lädt das persistente Marker-Lexikon (file-based).

    Format: {<airline>: {<doc_type>: {<first_token>: {meaning, activity_type,
    confirmed_count, conflicting_count, status, examples}}}}.
    """
    try:
        if os.path.isfile(_MARKER_LEXICON_PATH):
            with open(_MARKER_LEXICON_PATH, 'r') as f:
                return json.load(f) or {}
    except Exception as e:
        print(f'[marker_lexicon] load fail: {e}')
    return {}


def _save_marker_lexicon(lex):
    try:
        with open(_MARKER_LEXICON_PATH, 'w') as f:
            json.dump(lex, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print(f'[marker_lexicon] save fail: {e}')
        return False


def _record_marker_learning(airline, doc_type, first_token, meaning, activity_type,
                              job_id, datum, raw_marker):
    """v8.22 Rest-5: Speichert User-Antwort als learning_candidate. Bei
    wiederholter Bestätigung mit gleicher meaning → status=approved. Bei
    Konflikt (andere Erklärung) → status=conflict.

    Returns dict mit aktuellem Status für Audit.
    """
    if not first_token or not meaning:
        return None
    lex = _load_marker_lexicon()
    airline = (airline or 'LH').upper()
    doc_type = (doc_type or 'flugstundenuebersicht').lower()
    first_token = first_token.upper().strip()
    meaning = meaning.strip()[:80]
    activity_type = (activity_type or '').lower().strip()

    by_airline = lex.setdefault(airline, {})
    by_doc = by_airline.setdefault(doc_type, {})
    entry = by_doc.get(first_token)
    if entry is None:
        entry = {
            'meaning':           meaning,
            'activity_type':     activity_type,
            'confirmed_count':   1,
            'conflicting_count': 0,
            'status':            'pending_review',
            'first_seen_job_id': job_id,
            'examples':          [],
            'created_at':        datetime.utcnow().isoformat() + 'Z',
        }
        by_doc[first_token] = entry
    else:
        # Vergleich der bestehenden meaning vs neuer
        if entry.get('meaning', '').lower() == meaning.lower() \
                and entry.get('activity_type', '') == activity_type:
            entry['confirmed_count'] = int(entry.get('confirmed_count', 0) or 0) + 1
            # Auto-Approve bei 3+ konsistenten Bestätigungen
            if entry['confirmed_count'] >= 3 and entry.get('status') != 'approved':
                entry['status'] = 'approved'
                entry['approved_at'] = datetime.utcnow().isoformat() + 'Z'
        else:
            entry['conflicting_count'] = int(entry.get('conflicting_count', 0) or 0) + 1
            entry['status'] = 'conflict'

    examples = entry.setdefault('examples', [])
    examples.append({
        'job_id':       job_id, 'datum': datum,
        'raw_marker':   raw_marker, 'user_meaning': meaning,
        'user_activity_type': activity_type,
        'recorded_at':  datetime.utcnow().isoformat() + 'Z',
    })
    # Cap auf 20 Beispiele pro Eintrag
    if len(examples) > 20:
        entry['examples'] = examples[-20:]

    _save_marker_lexicon(lex)
    return {
        'first_token': first_token, 'status': entry['status'],
        'confirmed_count': entry['confirmed_count'],
        'conflicting_count': entry['conflicting_count'],
    }


def _make_short_code(token):
    """v8.22 Rest-1: kurzer ATX-XXXXX-Code (5 Zeichen ohne 0/O/1/I/L) abgeleitet
    aus Session-Token. Für User-Anzeige + Recovery via Code-Input.
    Kollisionsfrei pro Session weil deterministische Ableitung aus Token."""
    import hashlib as _hl
    alphabet = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'  # ohne 0,O,1,I,L
    h = _hl.sha256(token.encode()).digest()
    code = ''
    for i in range(5):
        code += alphabet[h[i] % len(alphabet)]
    return f'ATX-{code}'


def _save_session(token, data):
    """Speichert Session-Daten in Supabase (persistent) ODER Disk-Fallback."""
    expires = datetime.utcnow() + timedelta(hours=SESSION_HOURS)
    if SB_AVAILABLE:
        try:
            sb.table('sessions').upsert({
                'token':         token,
                'job_id':        data.get('job_id'),
                'result_data':   data.get('result_data'),
                'notes':         data.get('notes', []),
                'download_url':  data.get('download_url'),
                'chat_history':  data.get('chat_history', []),
                'expires_at':    expires.isoformat(),
            }).execute()
            return
        except Exception as e:
            print(f"[supabase] session save fail: {e} — fallback to disk")
    # Disk-Fallback
    payload = {**data, 'token': token, 'created': datetime.utcnow().isoformat() + 'Z',
               'expires': expires.isoformat() + 'Z'}
    try:
        with open(os.path.join(_SESSION_DIR, f'{token}.json'), 'w') as f:
            json.dump(payload, f, default=str)
    except Exception as e:
        print(f"[session] save fail: {e}")


def _sync_session_result_with_job(job_id, job_state):
    """v8.37: Spiegelt manual_day_overrides + aktualisierte review_items + preview_totals
    aus dem Job-State zurück in alle Sessions, die diesen job_id referenzieren.

    Damit überlebt der post-Review-Stand einen Browser-Reload / Recall.
    """
    if not job_id or not job_state: return
    job_data = job_state.get('data') or {}
    job_review_items = job_data.get('_review_items') or []
    overrides = job_state.get('manual_day_overrides') or {}
    preview = job_data.get('_preview_totals') or {}

    # Sessions finden, die job_id referenzieren
    if SB_AVAILABLE:
        try:
            res = sb.table('sessions').select('token,result_data').eq('job_id', job_id).execute()
            rows = (res and res.data) or []
            for row in rows:
                token = row.get('token')
                rd = dict(row.get('result_data') or {})
                rd['_review_items'] = job_review_items
                if preview:
                    # Live-Werte aktualisieren, damit Recall den neuen Betrag zeigt
                    if 'netto' in preview:    rd['netto']    = preview.get('netto')
                    if 'gesamt' in preview:   rd['gesamt']   = preview.get('gesamt')
                    if 'arbeitstage' in preview:    rd['arbeitstage']    = preview.get('arbeitstage')
                    if 'fahr_tage' in preview:      rd['fahr_tage']      = preview.get('fahr_tage')
                    if 'hotel_naechte' in preview:  rd['hotel_naechte']  = preview.get('hotel_naechte')
                    if 'vma_72_tage' in preview:    rd['vma_72_tage']    = preview.get('vma_72_tage')
                    if 'vma_73_tage' in preview:    rd['vma_73_tage']    = preview.get('vma_73_tage')
                    if 'vma_72' in preview:         rd['vma_72']         = preview.get('vma_72')
                    if 'vma_73' in preview:         rd['vma_73']         = preview.get('vma_73')
                    if 'vma_aus' in preview:        rd['vma_aus']        = preview.get('vma_aus')
                rd['_manual_day_overrides_count'] = len(overrides)
                sb.table('sessions').update({'result_data': rd}).eq('token', token).execute()
        except Exception as e:
            print(f'[session-sync] supabase fail: {e}')
            return
    # Disk-Fallback
    try:
        for fname in os.listdir(_SESSION_DIR):
            if not fname.endswith('.json'): continue
            fpath = os.path.join(_SESSION_DIR, fname)
            try:
                with open(fpath) as f: s = json.load(f)
            except Exception: continue
            if s.get('job_id') != job_id: continue
            rd = dict(s.get('result_data') or {})
            rd['_review_items'] = job_review_items
            if preview:
                for k in ('netto','gesamt','arbeitstage','fahr_tage','hotel_naechte',
                         'vma_72_tage','vma_73_tage','vma_72','vma_73','vma_aus'):
                    if k in preview: rd[k] = preview.get(k)
            rd['_manual_day_overrides_count'] = len(overrides)
            s['result_data'] = rd
            try:
                with open(fpath, 'w') as f: json.dump(s, f, default=str)
            except Exception: pass
    except Exception as e:
        print(f'[session-sync] disk fail: {e}')


def _cleanup_expired_sessions():
    """Löscht abgelaufene Sessions vom Disk. Wird periodisch ausgeführt."""
    try:
        now = datetime.utcnow()
        for fn in os.listdir(_SESSION_DIR):
            if not fn.endswith('.json'): continue
            path = os.path.join(_SESSION_DIR, fn)
            try:
                with open(path) as f: data = json.load(f)
                exp = datetime.fromisoformat(data.get('expires', '').replace('Z', ''))
                if exp < now:
                    os.remove(path)
                    print(f"[cleanup] expired session deleted: {fn[:24]}")
            except Exception:
                # Korrupte Datei → löschen
                os.remove(path)
    except Exception as e:
        print(f"[cleanup] failed: {e}")


def _cleanup_loop():
    """Background-Loop für regelmäßiges Cleanup (alle 30 Min) + Stale-Job-Detector (alle 2 Min)."""
    import time as _t
    cycle = 0
    while True:
        try:
            _t.sleep(120)  # 2 Min — häufig genug für Stale-Detection
            cycle += 1
            # v10.4: Stale-Job-Detector — bei jedem Cycle (alle 2 Min)
            _detect_and_fail_stale_jobs()
            # Vollständiger Cleanup nur alle 15 Cycles (≈ 30 Min)
            if cycle % 15 != 0:
                continue
            _cleanup_expired_sessions()
            # Auch alte Job-State-Files
            cutoff = datetime.utcnow() - timedelta(hours=48)
            for fn in os.listdir(_JOBS_DIR):
                if not fn.endswith('.json'): continue
                path = os.path.join(_JOBS_DIR, fn)
                try:
                    if datetime.utcfromtimestamp(os.path.getmtime(path)) < cutoff:
                        os.remove(path)
                        print(f"[cleanup] old job file deleted: {fn[:24]}")
                except: pass
            # Abgelaufene uploaded_files in Supabase löschen
            if SB_AVAILABLE:
                try:
                    now = datetime.utcnow().isoformat()
                    sb.table('uploaded_files').delete().lt('expires_at', now).execute()
                except Exception as e:
                    print(f"[cleanup] supabase uploaded_files: {e}")
                # Abgelaufene PDFs ebenfalls
                try:
                    sb.table('pdfs').delete().lt('expires_at', now).execute()
                except Exception as e:
                    print(f"[cleanup] supabase pdfs: {e}")
            # v10.3: Auch jobs (>7 Tage) + sessions (expired) cleanen
            cleanup_old_supabase_state()
        except: pass


# v10.4: Stale-Job-Detector — erkennt hängende Worker-Jobs und markiert sie als
# failed_timeout, damit die Queue nicht endlos blockiert ist und der User eine
# friendly-Fehlermeldung sieht.
_STALE_JOB_TIMEOUT_MIN = 10  # Job ohne Heartbeat-Update für 10 Min → failed_timeout
_STALE_JOB_GLOBAL_MAX_MIN = 15  # Job-Total-Runtime hart-Cap → failed_timeout


def _detect_and_fail_stale_jobs():
    """Iteriert über in-memory _jobs und failed Jobs deren Heartbeat zu alt ist.
    Wird im _cleanup_loop alle 2 Min aufgerufen — keine externen Calls."""
    try:
        now = datetime.utcnow()
        with _jobs_lock:
            stuck_ids = []
            for jid, j in _jobs.items():
                if j.get('status') != 'running':
                    continue
                # Check 1: Heartbeat zu alt?
                ph_ts = j.get('phase_updated_at') or j.get('created') or ''
                try:
                    ph_dt = datetime.fromisoformat(ph_ts.replace('Z', ''))
                    if (now - ph_dt) > timedelta(minutes=_STALE_JOB_TIMEOUT_MIN):
                        stuck_ids.append((jid, 'heartbeat_stale',
                                          int((now - ph_dt).total_seconds() / 60)))
                        continue
                except Exception:
                    pass
                # Check 2: Total-Runtime-Cap
                cr_ts = j.get('created') or ''
                try:
                    cr_dt = datetime.fromisoformat(cr_ts.replace('Z', ''))
                    if (now - cr_dt) > timedelta(minutes=_STALE_JOB_GLOBAL_MAX_MIN):
                        stuck_ids.append((jid, 'global_timeout',
                                          int((now - cr_dt).total_seconds() / 60)))
                except Exception:
                    pass
        # Failed setzen (außerhalb des Locks für minimale Lock-Zeit)
        for jid, reason, mins_stale in stuck_ids:
            with _jobs_lock:
                if jid not in _jobs: continue
                _jobs[jid]['status'] = 'failed_timeout'
                _jobs[jid]['error'] = (
                    'Die Auswertung wurde unterbrochen. '
                    'Bitte starte sie erneut — deine Auswertung ist nicht verloren.'
                )
                _jobs[jid]['failed_reason'] = reason
                _jobs[jid]['failed_at'] = now.isoformat() + 'Z'
            print(f"[stale-detector] Job {jid[:8]} failed_timeout: {reason} ({mins_stale} min stale)")
            try:
                _save_job_to_disk(jid)
            except Exception:
                pass
    except Exception as e:
        print(f"[stale-detector] error: {str(e)[:120]}")


def cleanup_old_supabase_state():
    """v10.3: Backend-Cleanup für Supabase-Tabellen jobs + sessions.
    Verhindert unbegrenztes Wachstum unabhängig von pg_cron.

    - sessions: löscht expires_at < now (24h Code-Validity).
    - jobs: löscht updated_at < now() - 7 Tage (Debug/Pilot-Window).
      Access-Code wird über sessions.expires_at geprüft, NICHT über jobs-Existenz —
      so kann Job intern 7 Tage liegen während Code nach 24h ungültig wird.

    Exception-safe — Cleanup-Loop läuft weiter selbst wenn ein Delete fehlschlägt.
    """
    if not SB_AVAILABLE:
        return
    sess_deleted = 0
    jobs_deleted = 0
    try:
        now_iso = datetime.utcnow().isoformat()
        try:
            res = sb.table('sessions').delete().lt('expires_at', now_iso).execute()
            sess_deleted = len((res and getattr(res, 'data', None)) or [])
        except Exception as e:
            print(f"[cleanup] supabase sessions delete fail: {str(e)[:120]}")
        try:
            cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
            res = sb.table('jobs').delete().lt('updated_at', cutoff).execute()
            jobs_deleted = len((res and getattr(res, 'data', None)) or [])
        except Exception as e:
            print(f"[cleanup] supabase jobs delete fail: {str(e)[:120]}")
        if sess_deleted or jobs_deleted:
            print(f"[cleanup] supabase state: {sess_deleted} sessions, {jobs_deleted} jobs (>7d) deleted")
        # v10.4: job_chunks ebenfalls cleanen (7-Tage Cutoff synchron zur jobs-TTL)
        try:
            cleanup_old_job_chunks()
        except Exception as e:
            print(f"[cleanup] job_chunks fail: {str(e)[:120]}")
    except Exception as e:
        # Outer catch — Cleanup-Loop niemals crashen lassen
        print(f"[cleanup] supabase state error: {str(e)[:120]}")


# Cleanup-Thread starten (in Tests deaktivierbar)
if os.environ.get('AEROTAX_DISABLE_BG_THREADS') != '1':
    __import__('threading').Thread(target=_cleanup_loop, daemon=True, name='cleanup-loop').start()


def _load_session(token):
    if not token: return None
    if SB_AVAILABLE:
        try:
            r = sb.table('sessions').select('*').eq('token', token).limit(1).execute()
            if r.data:
                row = r.data[0]
                # Expiry-Check
                try:
                    exp_str = (row.get('expires_at') or '').replace('Z', '').split('+')[0]
                    if datetime.fromisoformat(exp_str) < datetime.utcnow():
                        return None
                except: pass
                # Frontend-kompatible Form zurückgeben
                return {
                    'token': row.get('token'),
                    'job_id': row.get('job_id'),
                    'result_data': row.get('result_data') or {},
                    'notes': row.get('notes') or [],
                    'download_url': row.get('download_url'),
                    'chat_history': row.get('chat_history') or [],
                    'expires': row.get('expires_at'),
                }
        except Exception as e:
            print(f"[supabase] session load fail: {e} — fallback to disk")
    # Disk-Fallback
    path = os.path.join(_SESSION_DIR, f'{token}.json')
    if not os.path.exists(path): return None
    try:
        with open(path) as f:
            data = json.load(f)
        try:
            exp = datetime.fromisoformat(data['expires'].replace('Z', ''))
            if exp < datetime.utcnow():
                return None
        except: pass
        return data
    except: return None


@app.route('/api/session-by-code/<code>', methods=['GET'])
def session_by_short_code(code):
    """v8.22 Rest-1: Recovery via kurzem ATX-Code. Sucht alle aktiven Sessions
    deren _make_short_code(token) == code matched. Rate-limited per IP.
    """
    code = (code or '').strip().upper()
    if not code or not code.startswith('ATX-') or len(code) != 9:
        return jsonify({'error': 'Code muss Format ATX-XXXXX haben'}), 400
    # Rate-Limit pro IP (Brute-Force-Schutz)
    ip = (request.remote_addr or 'unknown')
    if not _qa_rate_check(ip, 'short-code', max_per_hour=20):
        return jsonify({'error': 'Zu viele Versuche — bitte später erneut.'}), 429

    # Iteriere über aktive sessions im store + supabase
    matched_token = None
    if SB_AVAILABLE:
        try:
            res = sb.table('sessions').select('token').execute()
            for row in (res.data or []):
                tok = row.get('token', '')
                if tok and _make_short_code(tok) == code:
                    matched_token = tok
                    break
        except Exception:
            pass
    if not matched_token:
        # Fallback: Disk-basierte Sessions durchsuchen
        try:
            import os as _o
            sessions_dir = _SESSION_DIR
            if _o.path.isdir(sessions_dir):
                for fn in _o.listdir(sessions_dir):
                    if not fn.endswith('.json'):
                        continue
                    tok = fn[:-5]
                    if _make_short_code(tok) == code:
                        matched_token = tok
                        break
        except Exception:
            pass
    if not matched_token:
        return jsonify({'error': 'Code unbekannt oder abgelaufen'}), 404
    return jsonify({'token': matched_token, 'short_code': code})


@app.route('/api/session/<token>', methods=['GET'])
def session_recall(token):
    """Holt Auswertungs-Ergebnis via Session-Token."""
    s = _load_session(token)
    if not s:
        return jsonify({'error': 'Session-Token ungültig oder abgelaufen'}), 404
    # Sensitiver Chat-Verlauf nicht standardmäßig zurückgeben
    safe = {k: v for k, v in s.items() if k != 'chat_history'}
    return jsonify(safe)


_OFF_TOPIC_PATTERNS = [
    # Promis / Personen
    r'\b(britney|spears|trump|biden|merkel|musk|bezos|messi|ronaldo|swift|beyon)',
    r'\bwer ist\b', r'\bwie heisst\b', r'\bwie heißt\b',
    # Geographie / Allgemeinwissen
    r'\bhauptstadt\b', r'\bbevölkerung\b', r'\beinwohner\b', r'\bgrößte stadt\b',
    # Politik
    r'\b(politik|wahl|partei|kanzler|präsident|bundestag|grüne|spd|cdu|csu|fdp|afd|linke)\b',
    # Promis allgemein
    r'\b(prominen|promi|stars?|filmstar|popstar|sänger|schauspieler)\b',
    # Investments
    r'\b(aktien?|krypto|bitcoin|ethereum|investiere|investment|vermögen|reich werden|reich\b|geld anlegen)\b',
    r'\bwie werde ich (reich|millionär|wohlhabend)\b',
    # Beziehungen / Lebensberatung
    r'\b(beziehung|freundin|freund|liebe|trennung|ehe|scheidung)\b',
    r'\b(was soll ich|wie werde ich|sollte ich)\b.{0,40}\b(machen|tun|werden|kaufen|kaufen)\b',
    # Programmierung / Tech
    r'\b(python|javascript|html|css|programmier|code\b|debug)\b',
    # Reise/Urlaub allgemein (NICHT dienstlich)
    r'\bbest(es)?\s+(restaurant|hotel|reise(ziel)?|urlaubsziel|sehenswürdigkeit)\b',
    # Medizin / Gesundheit Allgemein
    r'\b(krankheit|symptom|medikament|arzt empfehlen|diagnose)\b',
    # Generelle Wissensfragen
    r'\bwas ist (der|die|das)\s+(unterschied|sinn|grund)\b.{0,30}',
]


def _is_off_topic_question(message):
    """v8.22 Rest-4: Hard-Server-Pre-Filter für Off-Topic-Fragen.
    Greift VOR dem LLM-Call → kostenfrei + deterministisch.
    Returns True wenn die Frage offensichtlich off-topic ist.
    """
    import re as _re
    msg = (message or '').lower()
    # Erlaubte Crew/Steuer-Whitelist überschreibt Off-Topic-Match
    allowed_keywords = (
        'aerotax', 'wiso', 'auswertung', 'pdf', 'beleg', 'streckeneinsatz',
        'flugstunden', 'lohnsteuer', 'spesen', 'pauschale', 'fahrtkost',
        'reinigung', 'hotel', 'übernachtung', 'schulung', 'bürodienst',
        'office', 'training', 'tour', 'layover', 'ausland', 'inland',
        'werbungs', 'reisekost', 'verpfleg', 'gewerkschaft', 'crew',
        'flugbegleiter', 'pilot', 'kapit', 'einsatz', 'frei',
        'urlaub', 'krank', 'standby', 'simulator', 'sim ',
        'zugangscode', 'kurzcode', 'datei', 'dokument', 'unterlage',
        'briefing', 'off-duty', 'duty', 'lh ', 'lufthansa',
        'bmf', 'pauschale', 'tagessatz', 'antragsfrist',
    )
    for kw in allowed_keywords:
        if kw in msg:
            return False  # Crew/Steuer-Bezug → erlaubt
    # Kein Crew-Bezug → prüfe Off-Topic-Patterns
    for pat in _OFF_TOPIC_PATTERNS:
        if _re.search(pat, msg, _re.IGNORECASE):
            return True
    return False


@app.route('/api/chat', methods=['POST'])
def chat_with_aerotax():
    """Chat mit AeroTAX über deine Auswertung. Body: {token, message, kind?}."""
    body = request.get_json(silent=True) or {}
    token = body.get('token', '').strip()
    message = (body.get('message') or '').strip()[:2000]
    # v8.33: kind='review' → User ist im Review-Flow → keine Rate-Limits
    kind = (body.get('kind') or 'free').strip().lower()
    is_review_context = (kind == 'review')

    if not token or not message:
        return jsonify({'error': 'token und message erforderlich'}), 400
    # v8.33: kurze Antworten (z.B. „08:30", „?", „ja") sind im Review-Kontext valide
    if not is_review_context and len(message) < 3:
        return jsonify({'error': 'Frage zu kurz'}), 400

    # v8.22 Rest-4: Server-Pre-Filter — Off-Topic ohne LLM-Call ablehnen
    if _is_off_topic_question(message):
        off_topic_reply = (
            'Ich kann dir hier nur bei deiner AeroTAX-Auswertung helfen — '
            'also bei deinen Unterlagen, offenen Punkten, dem PDF und der '
            'Übernahme in deine Steuersoftware. Wenn du dazu eine Frage hast, '
            'bin ich da.'
        )
        return jsonify({'reply': off_topic_reply, 'filtered': 'off_topic'}), 200

    session = _load_session(token)
    if not session:
        return jsonify({'error': 'Session-Token ungültig oder abgelaufen — bitte neu auswerten'}), 401

    # ── COST-CONTROL: Hard-Caps pro Session ──────────────────
    # v8.33: Review-Kontext bypassed Caps & IP-Rate-Limit komplett
    chat_history_existing = session.get('chat_history', [])
    user_msg_count = sum(1 for m in chat_history_existing if m.get('role') == 'user' and not m.get('is_review'))
    HARD_CAP = 50
    if not is_review_context and user_msg_count >= HARD_CAP:
        return jsonify({
            'error': f'Maximum {HARD_CAP} freie Chat-Nachrichten pro Session erreicht. Review-Antworten und PDF-Erstellung gehen weiter.'
        }), 429

    # v8.34: IP-Rate-Limit komplett entfernt. Per-Session HARD_CAP=50 + Review-Bypass reicht.
    # Hauptauslöser für „Zu viele Nachrichten"-Frust war hier — User kriegt nie wieder diesen Block.

    if not ANTHROPIC_KEY:
        return jsonify({'error': 'KI nicht verfügbar'}), 503

    # Auswertungs-Daten in den Prompt einbauen
    result_data = session.get('result_data', {})
    chat_history = session.get('chat_history', [])
    notes = session.get('notes', [])

    summary_lines = [
        f"Mandant: {result_data.get('name','?')}",
        f"Steuerjahr: {result_data.get('year','?')}",
        f"Arbeitgeber: {result_data.get('arbeitgeber','Lufthansa')}",
        f"Brutto: {result_data.get('brutto', 0):.2f} €",
        f"Lohnsteuer: {result_data.get('lohnsteuer', 0):.2f} €",
        f"Z17 (AG-Fahrkostenzuschuss): {result_data.get('ag_z17', 0):.2f} €",
        f"Z77 (steuerfreie Spesen): {result_data.get('z77', 0):.2f} €",
        f"Fahrtkosten: {result_data.get('fahr', 0):.2f} € ({result_data.get('km',0)}km × {result_data.get('fahr_tage',0)} Fahrtage)",
        f"Reinigung: {result_data.get('reinig', 0):.2f} € ({result_data.get('reinigungstage', result_data.get('arbeitstage',0))} Reinigungstage × 1,60 €)",
        f"Trinkgelder: {result_data.get('trink', 0):.2f} € ({result_data.get('hotel_naechte',0)} Hotelnächte × 3,60 €)",
        f"Z72 (Inland >8h): {result_data.get('vma_72_tage',0)} Tage / {result_data.get('vma_72',0):.2f} €",
        f"Z73 (An-/Abreise): {result_data.get('vma_73_tage',0)} Tage / {result_data.get('vma_73',0):.2f} €",
        f"Z74 (Inland 24h): {result_data.get('vma_74_tage',0)} Tage / {result_data.get('vma_74',0):.2f} €",
        f"Z76 (Ausland-VMA): {result_data.get('vma_aus', 0):.2f} €",
        f"Brutto-Aufwendungen gesamt: {result_data.get('gesamt', 0):.2f} €",
        f"Einzutragender Gesamtbetrag: {result_data.get('netto', 0):.2f} €",
    ]

    notes_block = ('\n'.join(f"- {n}" for n in notes)) if notes else 'keine'
    history_block = '\n'.join(
        f"{'User' if m['role']=='user' else 'AeroTAX'}: {m['content']}"
        for m in chat_history[-10:]
    )

    # v8.33: Marker-Glossar + aktive Review-Gruppen — damit Sonnet nicht „Was bedeutet em?" fragt
    marker_glossary = (
        '- D4 = Schulung (z.B. Crew-Training, Recurrent)\n'
        '- EK = Bürodienst (z.B. ground duty, office)\n'
        '- SM = Seminar (mehrtägig)\n'
        '- EH = Erste-Hilfe-Schulung\n'
        '- EM = Emergency-Training\n'
        '- SIM = Simulator-Session\n'
        'Wenn der User „em", „eh", „d4" usw. schreibt, beziehe es auf diese Marker — frag NICHT nach.'
    )
    active_groups_block = ''
    try:
        review_items = result_data.get('_review_items') or []
        groups = _build_review_groups(review_items) if review_items else []
        if groups:
            lines = []
            for g in groups:
                lines.append(f"  - {g.get('group_id','?')}: {g.get('date_range','?')} "
                             f"— {g.get('label','?')} ({g.get('count',0)} Tage, "
                             f"Marker: {g.get('marker_summary','?')})")
            active_groups_block = '\n'.join(lines)
    except Exception:
        active_groups_block = ''

    prompt = f"""Du bist AeroTAX, der Werbungskosten-Auswertungs-Assistent von aerosteuer.de.
AeroTAX ist eine Berechnungs- und Dokumentationshilfe — KEINE Steuerberatung.

Du beantwortest STRENG NUR Fragen aus diesen erlaubten Themen:

  1. DIESER konkreten Auswertung des Nutzers (Werte, Berechnung, Plausibilität)
  2. WISO-Übernahme der Werte (welche Zeile, welcher Pfad)
  3. Hochgeladenen Dokumenten (Flugstundenübersicht, Streckeneinsatzabrechnung,
     Lohnsteuerbescheinigung) — was sie sind, warum sie gebraucht werden, was AeroTAX daraus liest
  4. Offene Punkte / Rückfragen (warum kann das PDF noch nicht erstellt werden,
     welches Dokument fehlt, Schulungs-/Office-Tag-Zeit)
  5. Wie der Nutzer mit seinem Zugangscode später zurückkommt

EXPLIZIT VERBOTEN:
  - Allgemeines Weltwissen (z.B. "Wie heißt Britney Spears?", "Hauptstadt von Frankreich")
  - Politik, Medizin, Beziehungen, Programmierung, Reiseplanung, Promis
  - Karriere-Beratung, Investments, Lebensberatung, was-wäre-wenn-Spiele
  - Hypothetische Steuer-Szenarien außerhalb dieser Auswertung
  - Andere Steuerjahre als das vorliegende
  - Steuerberatung im engeren Sinn (z.B. "ist das absetzbar?", "garantiert das Finanzamt akzeptiert?")

Bei Off-Topic-Fragen IMMER mit dieser Antwort ablehnen (höflich, einmal, dann Stopp):
"Ich kann dir hier nur bei deiner AeroTAX-Auswertung helfen — also bei deinen Unterlagen, offenen Punkten, dem PDF und der Übernahme in deine Steuersoftware. Wenn du dazu eine Frage hast, bin ich da."

KEINE allgemeinen Antworten geben.
KEINE Vermutungen aufstellen.
Bei Steuer-Zweifelsfragen außerhalb der Auswertung: auf Steuerberater oder Lohnsteuerhilfeverein verweisen.

Verboten: allgemeine Steuertipps, andere Jahre, Lebensberatung, Karriere, Investments, Politik, was-wäre-wenn-Spiele, hypothetische Beispiele.

═══ NUTZER-AUSWERTUNG (Steuerjahr {result_data.get('year','?')}) ═══
{chr(10).join(summary_lines)}

═══ MARKER-GLOSSAR (Lufthansa Crew-Marker) ═══
{marker_glossary}

═══ AKTIVE OFFENE GRUPPEN (für Review-Kontext) ═══
{active_groups_block or '(keine offenen Gruppen)'}

═══ HINWEISE AUS DER AUSWERTUNG ═══
{notes_block}

═══ BISHERIGER CHAT-VERLAUF (max 10 letzte) ═══
{history_block or '(erste Nachricht)'}

═══ NEUE FRAGE ═══
{message}

═══ ANTWORT-REGELN (STRENG, ZWINGEND) ═══
- MAX 4 Sätze. Kurz, freundlich, direkt.
- ABSOLUT VERBOTEN: Markdown-Tabellen (kein "| Spalte | ...", keine "---" Trennlinien)
- ABSOLUT VERBOTEN: Berechnungstabellen, Posten-Aufzählung ("27 km × 59 Tage = ...")
- ABSOLUT VERBOTEN: Pseudo-Headlines mit ** **, Trennlinien (---, ===), Emoji-Bullet-Headers ("✅", "📋", "**📋")
- ABSOLUT VERBOTEN das Wort "Netto:" mit Doppelpunkt direkt vor einem Betrag.
  Verwende "Einzutragender Gesamtbetrag" oder "vorläufiger Betrag".
- ABSOLUT VERBOTEN: "Netto in WISO", "einfach eintragen", "AeroTAX kennt deine Zahlen", "garantiert", "Mehr absetzen", "Steuerersparnis"
- ABSOLUT VERBOTEN: bei "wo finde ich..."-Fragen lange Erklärungen zu Hotelnächten/Reinigungstagen/Berechnung — User fragt nur nach EINEM Punkt
- Wenn der User „em", „eh", „d4", „ek", „sm" schreibt: NUTZE das MARKER-GLOSSAR oben. NICHT zurückfragen.
- Bei WISO-Frage: 1-Satz-Pfad (Ausgaben → Werbungskosten → Reisekosten → Zusammengefasste Auswärtstätigkeiten). Fertig.
- Disclaimer NUR wenn deine Antwort eine konkrete steuerliche Aussage trifft („§9", „absetzbar", Werteinterpretation).
  Bei Bedienfragen, „wo finde ich...", „wie übergebe ich...", „was bedeutet der Marker..." → KEIN Disclaimer.
- Wenn der vorherige Bot-Turn schon einen Disclaimer hatte, NIEMALS in derselben Konversation erneut.

═══ PFLICHT-DISCLAIMER bei steuerlichen Antworten (am Ende, neue Zeile) ═══
ℹ Hinweis: AeroTAX ist eine Berechnungs- und Dokumentationshilfe und ersetzt keine individuelle steuerliche Beratung."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        # Output-Cap: 600 Tokens = ca. 200-250 Wörter, hält Kosten klein
        resp = _claude_with_retry(client, 'claude-sonnet-4-6', 600, prompt,
                                   max_retries=2, label='Chat-AeroTAX')
        answer = resp.content[0].text.strip()

        # Chat-Verlauf updaten + speichern (v8.33: is_review-Flag mitschreiben)
        chat_history.append({
            'role': 'user', 'content': message,
            'ts': datetime.utcnow().isoformat() + 'Z',
            'is_review': is_review_context,
        })
        chat_history.append({
            'role': 'assistant', 'content': answer,
            'ts': datetime.utcnow().isoformat() + 'Z',
            'is_review': is_review_context,
        })
        session['chat_history'] = chat_history[-100:]  # max 100 Nachrichten (Review erhöht Volumen)
        _save_session(token, session)

        # Counter zählt nur freie Fragen (is_review=False)
        new_user_count = sum(1 for m in chat_history if m.get('role') == 'user' and not m.get('is_review'))
        remaining = max(0, HARD_CAP - new_user_count)
        return jsonify({
            'answer': answer,
            'remaining': remaining,
            'used': new_user_count,
            'cap': HARD_CAP,
            'is_review': is_review_context,
        })
    except Exception as e:
        print(f"[chat] failed: {e}")
        return jsonify({'error': f'Chat-Anfrage fehlgeschlagen: {str(e)[:200]}'}), 500


@app.route('/api/session/<token>/delete', methods=['POST'])
def session_delete(token):
    """User kann seine Daten manuell sofort löschen — Datenschutz auf Anforderung."""
    deleted = False
    if SB_AVAILABLE:
        try:
            sb.table('sessions').delete().eq('token', token).execute()
            deleted = True
        except Exception as e:
            print(f"[supabase] session delete fail: {e}")
    # Auch Disk-Fallback löschen
    path = os.path.join(_SESSION_DIR, f'{token}.json')
    if os.path.exists(path):
        try:
            os.remove(path)
            deleted = True
        except: pass
    return jsonify({'ok': True, 'deleted': deleted}), 200


@app.route('/api/chat/history', methods=['POST'])
def chat_history_get():
    """Gibt vollständigen Chat-Verlauf zurück."""
    body = request.get_json(silent=True) or {}
    token = body.get('token', '').strip()
    s = _load_session(token)
    if not s:
        return jsonify({'error': 'Session ungültig'}), 401
    return jsonify({'history': s.get('chat_history', [])})


@app.route('/api/chat/clear', methods=['POST'])
def chat_history_clear():
    """v9.6: Backdoor-Reset — User kann Chat neu starten ohne neue Auswertung.
    Body: {token}. Löscht chat_history (ABER nicht manual_day_overrides oder result_data)."""
    body = request.get_json(silent=True) or {}
    token = body.get('token', '').strip()
    s = _load_session(token)
    if not s:
        return jsonify({'error': 'Session ungültig'}), 401
    s['chat_history'] = []
    _save_session(token, s)
    return jsonify({'ok': True, 'cleared': True})


# ══════════════════════════════════════════════════════════════════
#  Q&A COMMUNITY — anonyme Fragen, Code-Namen, AeroTAX Auto-Antworten
# ══════════════════════════════════════════════════════════════════
import threading as _qa_thread

_QA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qa_data')
os.makedirs(_QA_DIR, exist_ok=True)
_QA_FILE = os.path.join(_QA_DIR, 'questions.json')
_QA_SEED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'qa_seed.json')
_qa_lock = _qa_thread.Lock()
_qa_rate = {}  # IP → [(timestamp, action), ...]
_QA_UPVOTE_DECAY_DAYS = 30  # Likes nur 30 Tage wert für Ranking, danach nur historisch


def _qa_seed_if_empty():
    """Bei leerer DB: Seed-Fragen einfügen (nur wenn Supabase leer ist)."""
    if not os.path.exists(_QA_SEED_FILE): return

    if SB_AVAILABLE:
        try:
            r = sb.table('questions').select('id').limit(1).execute()
            if r.data and len(r.data) > 0:
                return  # Schon gefüllt
            # DB leer → Seeds einfügen
            with open(_QA_SEED_FILE) as f:
                seeds = json.load(f)
            import random as _r
            now = datetime.utcnow()
            for i, s in enumerate(seeds):
                days_ago = _r.randint(1, 30)
                created = now - timedelta(days=days_ago, hours=_r.randint(0, 23), minutes=_r.randint(0, 59))
                answered = created + timedelta(seconds=_r.randint(20, 90))
                sb.table('questions').insert({
                    'codename': s.get('codename', 'Anonym'),
                    'title': s.get('title', ''),
                    'body': s.get('body', ''),
                    'tags': s.get('tags', []),
                    'aerotax_answer': s.get('aerotax_answer'),
                    'aerotax_answered_at': answered.isoformat() if s.get('aerotax_answer') else None,
                    'created_at': created.isoformat(),
                }).execute()
            print(f"[qa] Supabase seed loaded: {len(seeds)} questions")
            return
        except Exception as e:
            print(f"[qa] Supabase seed failed: {e} — fallback to file")

    # Disk-Fallback
    if os.path.exists(_QA_FILE):
        try:
            with open(_QA_FILE) as f:
                data = json.load(f)
            if isinstance(data, list) and len(data) > 0: return
        except: pass
    try:
        with open(_QA_SEED_FILE) as f:
            seeds = json.load(f)
        import random as _r
        now = datetime.utcnow()
        out = []
        for i, s in enumerate(seeds):
            days_ago = _r.randint(1, 30)
            created = now - timedelta(days=days_ago, hours=_r.randint(0, 23), minutes=_r.randint(0, 59))
            answered = created + timedelta(seconds=_r.randint(20, 90))
            q = {
                'id': str(uuid.uuid4()),
                'codename': s.get('codename', 'Anonym'),
                'title': s.get('title', ''),
                'body': s.get('body', ''),
                'tags': s.get('tags', []),
                'created': created.isoformat() + 'Z',
                'upvotes_log': [],
                'answers': [],
                'aerotax_answer': s.get('aerotax_answer'),
                'aerotax_answered_at': answered.isoformat() + 'Z' if s.get('aerotax_answer') else None,
            }
            out.append(q)
        with open(_QA_FILE, 'w') as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"[qa] Disk-Fallback seed loaded: {len(out)} questions")
    except Exception as e:
        print(f"[qa] Seed failed: {e}")


def _qa_effective_upvotes(log):
    """Anzahl Upvotes innerhalb der letzten 30 Tage. Decay-basiertes Ranking."""
    if not log: return 0
    cutoff = datetime.utcnow() - timedelta(days=_QA_UPVOTE_DECAY_DAYS)
    count = 0
    for v in log:
        try:
            ts = v.get('ts', '').replace('Z', '')
            if datetime.fromisoformat(ts) >= cutoff:
                count += 1
        except: pass
    return count


def _qa_total_upvotes(log):
    """Gesamt-Upvotes, inklusive älter als 30 Tage (Historie)."""
    return len(log) if log else 0


def _qa_load():
    _qa_seed_if_empty()
    if not os.path.exists(_QA_FILE): return []
    try:
        with open(_QA_FILE) as f: return json.load(f)
    except: return []


def _qa_save(questions):
    try:
        with open(_QA_FILE, 'w') as f: json.dump(questions, f, ensure_ascii=False, indent=1)
    except Exception as e: print(f"[qa] save failed: {e}")


def _qa_random_codename():
    """Generiert anonymen Code-Namen wenn User keinen angibt."""
    import random as _r
    adj = ['Flying','Sky','Cloud','Jet','Aero','Cruising','Mach','Vector','Heading','Turning','Climbing','Descending','Boarding','Cabin','Wing']
    noun = ['Falcon','Eagle','Hawk','Phoenix','Comet','Star','Pilot','Wanderer','Drifter','Voyager','Captain','Crew','Skywalker','Nomad','Dreamer']
    return f"{_r.choice(adj)}{_r.choice(noun)}{_r.randint(10,99)}"


def _qa_rate_check(ip, action, max_per_hour):
    """Rate-Limit pro IP. Returnt True wenn erlaubt."""
    now = datetime.utcnow()
    key = f'{ip}:{action}'
    with _qa_lock:
        history = _qa_rate.get(key, [])
        # filter on last hour
        history = [t for t in history if (now - t).total_seconds() < 3600]
        if len(history) >= max_per_hour:
            _qa_rate[key] = history
            return False
        history.append(now)
        _qa_rate[key] = history
        # Periodisches Aufräumen — leere Einträge entfernen, damit dict nicht unbounded wächst
        if len(_qa_rate) > 5000:
            _qa_rate_cleanup(now)
        return True


def _qa_rate_cleanup(now):
    """Entfernt abgelaufene Rate-Limit-Einträge (intern, ruft mit _qa_lock gehalten auf)."""
    expired = [k for k, h in _qa_rate.items()
               if not h or (now - h[-1]).total_seconds() >= 3600]
    for k in expired:
        _qa_rate.pop(k, None)


def _qa_aerotax_answer(question_title, question_body):
    """Generiert AeroTAX-Bot-Antwort via Claude. Wird automatisch zu jeder Frage gerufen."""
    if not ANTHROPIC_KEY: return None
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        # EASA-Referenz mitschicken für fundierte Antworten
        easa_kontext = ''
        try:
            ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_easa.txt')
            if os.path.exists(ref):
                with open(ref, encoding='utf-8') as f:
                    easa_kontext = f.read()
        except: pass

        prompt = f"""Du bist AeroTAX — der Tax-Advisor-Bot von aerosteuer.de für Lufthansa-Kabinenpersonal und Cockpit-Crew.
Beantworte die folgende Community-Frage kurz, präzise, fundiert. Nutze §9 EStG, EASA-FTL, BMF-Schreiben als Wissensbasis.

═══ FACHWISSEN (zur Referenz, nicht zitieren) ═══
{easa_kontext[:8000]}

═══ COMMUNITY-FRAGE ═══
Titel: {question_title}
Frage: {question_body}

═══ ANTWORT-RICHTLINIEN ═══
- 3-6 Absätze, klar strukturiert
- Konkrete Werte/Pauschalen nennen wenn relevant (mit Jahr-Hinweis)
- Keine "Hier ist die Antwort"-Floskeln, direkt einsteigen
- Wenn Frage außerhalb Steuerrecht: höflich auf Steuer-Fokus hinweisen
- Tonalität: kollegial, hilfreich, fachlich. Nicht von oben herab.
- Wenn unklar oder mehrdeutig: explizit darauf hinweisen
- Quellen verweisen wenn möglich (§ EStG-Paragraph, BMF-Schreiben Datum)

═══ PFLICHT-ABSCHLUSS (immer am Ende der Antwort wörtlich anhängen, mit Leerzeile davor) ═══
ℹ Hinweis: AeroTAX ist eine Berechnungs- und Dokumentationshilfe und ersetzt keine individuelle steuerliche Beratung. Bei komplexen Einzelfällen ziehe einen Steuerberater oder Lohnsteuerhilfeverein zu Rate.

Antworte direkt mit dem Antworttext (kein Header, kein "Hallo X")."""
        resp = _claude_with_retry(client, 'claude-sonnet-4-6', 1200, prompt,
                                   max_retries=2, label='AeroTAX-QA')
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[qa] AeroTAX answer failed: {e}")
        return None


def _qa_async_aerotax(qid, title, body):
    """Background-Thread: AeroTAX antwortet, schreibt zurück."""
    answer = _qa_aerotax_answer(title, body)
    if not answer: return
    if SB_AVAILABLE:
        try:
            sb.table('questions').update({
                'aerotax_answer': answer,
                'aerotax_answered_at': datetime.utcnow().isoformat(),
            }).eq('id', qid).execute()
            return
        except Exception as e:
            print(f"[supabase] aerotax-answer save fail: {e}")
    # Disk-Fallback
    with _qa_lock:
        questions = _qa_load()
        for q in questions:
            if q['id'] == qid:
                q['aerotax_answer'] = answer
                q['aerotax_answered_at'] = datetime.utcnow().isoformat() + 'Z'
                _qa_save(questions)
                break


def _qa_load_from_sb():
    """Lädt alle Fragen + Antworten + Upvotes aus Supabase, vereint sie zur Frontend-Form."""
    try:
        # Fragen
        qr = sb.table('questions').select('*').order('created_at', desc=True).limit(200).execute()
        questions = qr.data or []
        if not questions: return []
        qids = [q['id'] for q in questions]
        # Antworten
        ar = sb.table('answers').select('*').in_('question_id', qids).execute()
        answers_by_qid = {}
        for a in (ar.data or []):
            answers_by_qid.setdefault(a['question_id'], []).append(a)
        # Upvotes (alle, last 60 Tage als Cap für Performance)
        cutoff = (datetime.utcnow() - timedelta(days=60)).isoformat()
        ur = sb.table('upvotes').select('*').gte('created_at', cutoff).execute()
        upvotes_by_target = {}
        for v in (ur.data or []):
            key = (v['target_type'], v['target_id'])
            upvotes_by_target.setdefault(key, []).append({'ts': v['created_at'], 'h': v['ip_hash']})

        # Frontend-kompatible Form bauen
        out = []
        for q in questions:
            qkey = ('question', q['id'])
            q_upvotes = upvotes_by_target.get(qkey, [])
            ans_list = []
            for a in sorted(answers_by_qid.get(q['id'], []), key=lambda x: x['created_at']):
                akey = ('answer', a['id'])
                a_upvotes = upvotes_by_target.get(akey, [])
                ans_list.append({
                    'id': a['id'],
                    'codename': a['codename'],
                    'body': a['body'],
                    'reply_to': a.get('reply_to'),
                    'created': a['created_at'],
                    'upvotes_log': a_upvotes,
                })
            out.append({
                'id': q['id'],
                'codename': q['codename'],
                'title': q['title'],
                'body': q['body'],
                'tags': q.get('tags') or [],
                'created': q['created_at'],
                'upvotes_log': q_upvotes,
                'answers': ans_list,
                'aerotax_answer': q.get('aerotax_answer'),
                'aerotax_answered_at': q.get('aerotax_answered_at'),
            })
        return out
    except Exception as e:
        print(f"[supabase] qa load fail: {e}")
        return None


@app.route('/api/qa', methods=['GET'])
def qa_list():
    """Liste aller Fragen. Query params: sort (hot/top/new), q, tag, limit."""
    sort_mode = request.args.get('sort', 'hot').lower()
    keyword = (request.args.get('q', '') or '').strip().lower()
    tag = (request.args.get('tag', '') or '').strip().lower()
    limit = min(int(request.args.get('limit', 50)), 100)

    if SB_AVAILABLE:
        # Sicherstellen dass Seeds drin sind (nur einmalig)
        _qa_seed_if_empty()
        questions = _qa_load_from_sb()
        if questions is None:
            questions = []
    else:
        with _qa_lock:
            questions = _qa_load()

    # Effective upvotes pro Frage berechnen + Frontend-Felder anreichern
    enriched = []
    for q in questions:
        # Schema-Migration: alte 'upvotes' int → 'upvotes_log'
        if 'upvotes_log' not in q and 'upvotes' in q:
            q['upvotes_log'] = [{'ts': q.get('created', datetime.utcnow().isoformat()+'Z')} for _ in range(q.get('upvotes', 0))]
        log = q.get('upvotes_log', [])
        q['upvotes_30d'] = _qa_effective_upvotes(log)
        q['upvotes_total'] = _qa_total_upvotes(log)
        # Antworten ebenfalls
        for a in q.get('answers', []):
            if 'upvotes_log' not in a and 'upvotes' in a:
                a['upvotes_log'] = [{'ts': a.get('created', datetime.utcnow().isoformat()+'Z')} for _ in range(a.get('upvotes', 0))]
            alog = a.get('upvotes_log', [])
            a['upvotes_30d'] = _qa_effective_upvotes(alog)
            a['upvotes_total'] = _qa_total_upvotes(alog)
        enriched.append(q)

    # Filter
    filtered = enriched
    if keyword:
        filtered = [q for q in filtered if keyword in (q.get('title','')+' '+q.get('body','')).lower()]
    if tag:
        filtered = [q for q in filtered if tag in [t.lower() for t in q.get('tags', [])]]

    # Sort
    if sort_mode == 'new':
        filtered.sort(key=lambda q: q.get('created', ''), reverse=True)
    elif sort_mode == 'top':
        filtered.sort(key=lambda q: q.get('upvotes_total', 0), reverse=True)
    else:  # hot — Decay-weighted: 30d upvotes + recent boost
        def hot_score(q):
            ev = q.get('upvotes_30d', 0)
            try:
                age_h = (datetime.utcnow() - datetime.fromisoformat(q.get('created', '').replace('Z',''))).total_seconds() / 3600
            except: age_h = 999
            recency_bonus = max(0, 5 - age_h / 24)  # Frische Fragen-Boost erste 5 Tage
            return ev * 2 + recency_bonus
        filtered.sort(key=hot_score, reverse=True)

    # Top tags global
    all_tags = {}
    for q in enriched:
        for t in q.get('tags', []):
            all_tags[t] = all_tags.get(t, 0) + 1
    top_tags = sorted(all_tags.items(), key=lambda x: x[1], reverse=True)[:15]

    return jsonify({
        'questions': filtered[:limit],
        'total': len(filtered),
        'all_tags': [{'tag': t, 'count': c} for t, c in top_tags],
        'sort': sort_mode,
    })


@app.route('/api/qa/ask', methods=['POST'])
def qa_ask():
    """Neue Frage stellen. Body: {title, body, codename?, tags?}."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    if not _qa_rate_check(ip, 'ask', max_per_hour=5):
        return jsonify({'error': 'Zu viele Fragen — bitte warte eine Stunde'}), 429
    body = request.get_json(silent=True) or {}
    title = (body.get('title') or '').strip()[:200]
    text = (body.get('body') or '').strip()[:5000]
    if len(title) < 5 or len(text) < 10:
        return jsonify({'error': 'Titel min. 5 Zeichen, Frage min. 10 Zeichen'}), 400
    codename = (body.get('codename') or '').strip()[:30] or _qa_random_codename()
    tags = body.get('tags') or []
    if not isinstance(tags, list): tags = []
    tags = [str(t).strip()[:20] for t in tags[:5] if t]

    qid = str(uuid.uuid4())
    created_at_iso = datetime.utcnow().isoformat()

    if SB_AVAILABLE:
        try:
            sb.table('questions').insert({
                'id': qid,
                'codename': codename,
                'title': title,
                'body': text,
                'tags': tags,
                'created_at': created_at_iso,
            }).execute()
        except Exception as e:
            print(f"[supabase] qa_ask fail: {e}")
            return jsonify({'error': 'Speichern fehlgeschlagen — bitte später nochmal'}), 500
    else:
        question_obj = {
            'id': qid, 'codename': codename, 'title': title, 'body': text, 'tags': tags,
            'created': created_at_iso + 'Z', 'upvotes_log': [], 'answers': [],
            'aerotax_answer': None, 'aerotax_answered_at': None,
        }
        with _qa_lock:
            questions = _qa_load()
            questions.append(question_obj)
            _qa_save(questions)

    # AeroTAX antwortet im Hintergrund
    _qa_thread.Thread(target=_qa_async_aerotax, args=(qid, title, text), daemon=True).start()

    return jsonify({'ok': True, 'question': {'id': qid, 'codename': codename, 'title': title, 'body': text, 'tags': tags, 'created': created_at_iso}})


@app.route('/api/qa/<qid>/answer', methods=['POST'])
def qa_answer(qid):
    """Community-Antwort zu einer Frage."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    if not _qa_rate_check(ip, 'answer', max_per_hour=20):
        return jsonify({'error': 'Zu viele Antworten — bitte warte eine Stunde'}), 429
    body = request.get_json(silent=True) or {}
    text = (body.get('body') or '').strip()[:3000]
    if len(text) < 10:
        return jsonify({'error': 'Antwort min. 10 Zeichen'}), 400
    codename = (body.get('codename') or '').strip()[:30] or _qa_random_codename()

    aid = str(uuid.uuid4())
    created_iso = datetime.utcnow().isoformat()
    answer = {
        'id': aid, 'codename': codename, 'body': text,
        'created': created_iso, 'upvotes_log': [],
        'reply_to': body.get('reply_to'),
    }

    if SB_AVAILABLE:
        try:
            # Erst prüfen ob Frage existiert
            qcheck = sb.table('questions').select('id').eq('id', qid).limit(1).execute()
            if not qcheck.data:
                return jsonify({'error': 'Frage nicht gefunden'}), 404
            sb.table('answers').insert({
                'id': aid,
                'question_id': qid,
                'codename': codename,
                'body': text,
                'reply_to': body.get('reply_to'),
                'created_at': created_iso,
            }).execute()
            return jsonify({'ok': True, 'answer': answer})
        except Exception as e:
            print(f"[supabase] qa_answer fail: {e}")
            return jsonify({'error': 'Speichern fehlgeschlagen'}), 500

    # Disk-Fallback
    with _qa_lock:
        questions = _qa_load()
        for q in questions:
            if q['id'] == qid:
                q['answers'].append(answer)
                _qa_save(questions)
                return jsonify({'ok': True, 'answer': answer})
    return jsonify({'error': 'Frage nicht gefunden'}), 404


@app.route('/api/support-message', methods=['POST'])
def support_message():
    """Speichert eine Support-Anfrage in Supabase (oder Disk-Fallback)."""
    body = request.get_json(silent=True) or {}
    reason  = (body.get('reason') or '').strip()[:120]
    email   = (body.get('email')  or '').strip()[:200]
    phone   = (body.get('phone')  or '').strip()[:80]
    message = (body.get('message') or '').strip()[:5000]

    if not reason or not email or not message:
        return jsonify({'error': 'Pflichtfelder fehlen'}), 400
    # Simple email check
    if '@' not in email or '.' not in email.split('@')[-1]:
        return jsonify({'error': 'E-Mail-Format ungültig'}), 400

    record = {
        'reason': reason,
        'email': email,
        'phone': phone,
        'message': message,
        'created_at': datetime.utcnow().isoformat() + 'Z',
        'ip_hash': _hashlib.sha256(
            (request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip()
             + os.environ.get('RECOVERY_SECRET','')).encode()
        ).hexdigest()[:12],
    }

    saved = False
    if SB_AVAILABLE:
        try:
            sb.table('support_requests').insert(record).execute()
            saved = True
            print(f"[support] saved to Supabase: {reason} from {email}")
        except Exception as e:
            print(f"[support] Supabase insert fail: {e}")

    if not saved:
        # Disk-Fallback
        try:
            sup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'support_inbox')
            os.makedirs(sup_dir, exist_ok=True)
            fname = f"{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{record['ip_hash']}.json"
            with open(os.path.join(sup_dir, fname), 'w') as f:
                json.dump(record, f, indent=2, ensure_ascii=False)
            saved = True
            print(f"[support] saved to disk: {fname}")
        except Exception as e:
            print(f"[support] disk save fail: {e}")
            return jsonify({'error': 'Speichern fehlgeschlagen'}), 500

    # ── Email-Notification an Admin via Resend (best-effort, blockt nicht) ──
    _send_support_email_notification(record)

    return jsonify({'ok': True, 'message': 'Nachricht erhalten — wir melden uns'})


def _send_support_email_notification(record):
    """Schickt eine Notification-Mail an Admin via Resend API.
    Failures werden nur geloggt — User-Submit gilt als erfolgreich auch ohne Mail.
    """
    api_key = os.environ.get('RESEND_API_KEY', '').strip()
    to_email = os.environ.get('SUPPORT_NOTIFY_EMAIL', 'miguel.schumann@icloud.com').strip()
    if not api_key:
        print("[support-mail] RESEND_API_KEY nicht gesetzt — überspringe Email-Notification")
        return
    try:
        import urllib.request, urllib.error
        subject = f"[AeroTAX Support] {record.get('reason','—')} · {record.get('email','')}"
        html_body = (
            f"<h2 style='font-family:sans-serif'>Neue Support-Anfrage</h2>"
            f"<p style='font-family:sans-serif;color:#444'>"
            f"<b>Grund:</b> {record.get('reason','')}<br>"
            f"<b>Email:</b> <a href='mailto:{record.get('email','')}'>{record.get('email','')}</a><br>"
            f"<b>Telefon:</b> {record.get('phone','') or '—'}<br>"
            f"<b>Eingegangen:</b> {record.get('created_at','')}<br>"
            f"<b>IP-Hash:</b> {record.get('ip_hash','')}"
            f"</p>"
            f"<div style='background:#f5f5f7;border-radius:8px;padding:16px;font-family:sans-serif;white-space:pre-wrap;color:#222;border-left:3px solid #2563eb'>"
            f"{(record.get('message','') or '').replace('<','&lt;').replace('>','&gt;')}"
            f"</div>"
            f"<p style='font-family:sans-serif;font-size:12px;color:#888;margin-top:20px'>"
            f"Antworte direkt auf diese Mail — sie geht an <b>{record.get('email','')}</b> raus."
            f"</p>"
        )
        payload = json.dumps({
            'from': 'AeroTAX Support <support@aerosteuer.de>',
            'to': [to_email],
            'reply_to': record.get('email',''),
            'subject': subject,
            'html': html_body,
        }).encode()
        req = urllib.request.Request(
            'https://api.resend.com/emails',
            data=payload,
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if 200 <= resp.status < 300:
                print(f"[support-mail] sent to {to_email}")
            else:
                print(f"[support-mail] unexpected status {resp.status}")
    except Exception as e:
        print(f"[support-mail] send fail: {e}")


@app.route('/api/admin/support-list', methods=['GET'])
def admin_support_list():
    """Liest gespeicherte Support-Anfragen — geschützt durch Token-Header."""
    auth = request.headers.get('X-Admin-Token', '')
    expected = os.environ.get('RECOVERY_SECRET', '')
    if not expected or auth != expected:
        return jsonify({'error': 'Unauthorized'}), 401

    items = []
    if SB_AVAILABLE:
        try:
            r = sb.table('support_requests').select('*').order('created_at', desc=True).limit(200).execute()
            items.extend(r.data or [])
        except Exception as e:
            print(f"[admin] supabase support-list fail: {e}")

    # Disk-Fallback dazu
    try:
        sup_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'support_inbox')
        if os.path.isdir(sup_dir):
            for fname in sorted(os.listdir(sup_dir), reverse=True)[:200]:
                if not fname.endswith('.json'): continue
                try:
                    with open(os.path.join(sup_dir, fname)) as f:
                        items.append(json.load(f))
                except: pass
    except Exception as e:
        print(f"[admin] disk support-list fail: {e}")

    return jsonify({'count': len(items), 'items': items})


@app.route('/api/qa/<qid>/upvote', methods=['POST'])
def qa_upvote(qid):
    """Upvote für Frage oder Antwort. Body: {answer_id?}."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    if not _qa_rate_check(ip, 'upvote', max_per_hour=60):
        return jsonify({'error': 'Zu viele Upvotes — bitte warte'}), 429
    body = request.get_json(silent=True) or {}
    answer_id = body.get('answer_id')
    ip_hash = _hashlib.sha256((ip + os.environ.get('RECOVERY_SECRET','')).encode()).hexdigest()[:8]
    target_type = 'answer' if answer_id else 'question'
    target_id = answer_id if answer_id else qid

    if SB_AVAILABLE:
        try:
            # Minimaler Spam-Schutz: 1-Sekunden-Window — User kann frei mehrfach liken
            cutoff = (datetime.utcnow() - timedelta(seconds=1)).isoformat()
            check = sb.table('upvotes').select('id').eq('target_type', target_type).eq('target_id', target_id).eq('ip_hash', ip_hash).gte('created_at', cutoff).limit(1).execute()
            if check.data:
                return jsonify({'error': 'Bitte langsamer klicken'}), 429
            # Alte Votes derselben IP fürs selbe Target löschen (>1s alt) — UNIQUE-Constraint Workaround
            try:
                sb.table('upvotes').delete().eq('target_type', target_type).eq('target_id', target_id).eq('ip_hash', ip_hash).lt('created_at', cutoff).execute()
            except Exception as _de:
                print(f"[supabase] cleanup old votes fail: {_de}")
            sb.table('upvotes').insert({
                'target_type': target_type,
                'target_id': target_id,
                'ip_hash': ip_hash,
            }).execute()
            # Counts zurückgeben
            cutoff_30d = (datetime.utcnow() - timedelta(days=30)).isoformat()
            r30 = sb.table('upvotes').select('id', count='exact').eq('target_type', target_type).eq('target_id', target_id).gte('created_at', cutoff_30d).execute()
            rtotal = sb.table('upvotes').select('id', count='exact').eq('target_type', target_type).eq('target_id', target_id).execute()
            return jsonify({
                'ok': True,
                'upvotes_30d': r30.count or 0,
                'upvotes_total': rtotal.count or 0,
            })
        except Exception as e:
            err_str = str(e).lower()
            if 'unique' in err_str or 'duplicate' in err_str:
                return jsonify({'error': 'Schon gevotet — bitte 1 Stunde warten'}), 429
            print(f"[supabase] upvote fail: {e}")
            return jsonify({'error': 'Speichern fehlgeschlagen'}), 500

    # Disk-Fallback
    vote = {'ts': datetime.utcnow().isoformat() + 'Z', 'h': ip_hash}
    with _qa_lock:
        questions = _qa_load()
        for q in questions:
            if q['id'] == qid:
                target = None
                if answer_id:
                    for a in q.get('answers', []):
                        if a['id'] == answer_id:
                            target = a; break
                else:
                    target = q
                if not target:
                    return jsonify({'error': 'Antwort nicht gefunden'}), 404
                target.setdefault('upvotes_log', [])
                cutoff = datetime.utcnow() - timedelta(seconds=1)
                already_voted = any(
                    v.get('h') == ip_hash and datetime.fromisoformat(v.get('ts','').replace('Z','')) >= cutoff
                    for v in target['upvotes_log']
                )
                if already_voted:
                    return jsonify({'error': 'Bitte langsamer klicken'}), 429
                target['upvotes_log'].append(vote)
                _qa_save(questions)
                return jsonify({
                    'ok': True,
                    'upvotes_30d': _qa_effective_upvotes(target['upvotes_log']),
                    'upvotes_total': _qa_total_upvotes(target['upvotes_log']),
                })
    return jsonify({'error': 'Nicht gefunden'}), 404


@app.route('/')
def health():
    return jsonify({
        'status':  'AeroTax Backend läuft',
        'version': APP_VERSION,
        'build':   APP_BUILD,
        'reader_versions': READER_VERSIONS,
        'engine':  ENGINE_VERSION,
        'prompt_version': PROMPT_VERSION,
        'bmf_data_year': 2025,
    })


@app.route('/api/progress', methods=['GET'])
def progress_stream():
    """Server-Sent Events — live Fortschritt während Claude rechnet.
    Hauptsteps für die ersten ~2 Min, danach Heartbeats alle 10s ('noch dabei…')
    bis maximal 8 Minuten — der Frontend-Timeout schließt vorher.
    """
    year = request.args.get('year', 'Steuerjahr')
    def generate():
        steps = [
            (5,  'Dokumente werden geöffnet…'),
            (10, 'Lohnsteuerbescheinigung wird gelesen…'),
            (16, f'Streckeneinsatz {year} wird analysiert…'),
            (22, 'KI liest Flugstunden Monat für Monat…'),
            (28, 'Fahrtage werden gezählt…'),
            (34, 'Hotelnächte nach EASA-FTL geprüft…'),
            (40, 'Auslandsrouten + BMF-Pauschalen…'),
            (46, 'Steuerfreie Spesen werden berechnet…'),
            (52, 'Fahrtkosten werden ermittelt…'),
            (58, 'Belege werden ausgewertet…'),
            (64, 'Netto-Betrag wird berechnet…'),
            (70, 'PDF wird erstellt…'),
        ]
        # Heartbeat-Texte für die längere Wartezeit (rotieren alle 10s)
        wait_msgs = [
            'KI prüft Tag für Tag — bitte einen Moment…',
            'Bin noch dabei, deine Auswertung wird gerade fertig…',
            'Konsistenz-Check der Werte…',
            'Letzte Plausi-Checks laufen…',
            'Gleich fertig — Ergebnis wird formatiert…',
        ]
        import time, json as _j

        # Phase 1: Hauptsteps (12s Abstand → ~2:24 Min)
        for pct, text in steps:
            yield f"data: {_j.dumps({'pct': pct, 'text': text})}\n\n"
            time.sleep(12)

        # Phase 2: Heartbeat (alle 10s, langsam steigend bis 95%)
        # Maximum 30 Heartbeats → 5 Min zusätzlich → Gesamt ~7:30 Min
        pct = 72
        for i in range(30):
            msg = wait_msgs[i % len(wait_msgs)]
            yield f"data: {_j.dumps({'pct': pct, 'text': msg})}\n\n"
            time.sleep(10)
            if pct < 95:
                pct += 1

        yield f"data: {_j.dumps({'pct': 100, 'text': 'Fertig!'})}\n\n"
    return app.response_class(generate(), mimetype='text/event-stream',
        headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})



# ══════════════════════════════════════════════════════════════════
#  KI-PARSER — liest die Lufthansa PDFs
# ══════════════════════════════════════════════════════════════════

def _bytes_list(file_list):
    """Normalisiert eine Liste aus reinen bytes ODER (bytes, filename) Tupeln → Liste von bytes."""
    result = []
    for item in (file_list or []):
        if isinstance(item, tuple):
            result.append(item[0])
        else:
            result.append(item)
    return result

def _bytes_filename_list(file_list):
    """Normalisiert → Liste von (bytes, filename) Tupeln."""
    result = []
    for item in (file_list or []):
        if isinstance(item, tuple):
            result.append(item)
        else:
            result.append((item, ''))
    return result

def _lsb_extract_via_regex(text):
    """Liefert numerische Werte direkt aus dem PDF-Text via Regex.
    Nullwerte = nicht gefunden / Format unbekannt."""
    def find(pattern, default=0.0):
        m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if m:
            try: return float(m.group(1).replace('.','').replace(',','.'))
            except: pass
        return default

    out = {}
    # Brutto: mehrere Patterns für Format-Robustheit
    b = find(r'Bruttoarbeitslohn[^\d]+([\d\.]+,\d{2})')
    if b == 0: b = find(r'3\.\s*Bruttoarbeitslohn\s+([\d\.]+,\d{2})')
    if b == 0: b = find(r'Bruttoarbeitslohn[\s\S]{0,200}?(\d{2,6}[\.,]\d{2})')
    if b == 0: b = find(r'Arbeitslohn[^\d]+(\d{2,6}[\.,]\d{2})')
    out['brutto'] = b
    out['lohnsteuer'] = find(r'Lohnsteuer von 3\.[^\d]+([\d\.]+,\d{2})')
    out['soli']       = find(r'Solidarit[^\d]+([\d\.]+,\d{2})')
    out['kirchensteuer_an'] = find(r'Kirchensteuer des\nArbeitnehmers von 3\.[^\d]+([\d\.]+,\d{2})')
    out['ag_fahrt_z17']     = find(r'Entfernungspauschale anzurechnen sind\s+([\d\.]+,\d{2})')
    out['ag_fahrt_z18_pauschal'] = find(r'15%[^\d]+([\d\.]+,\d{2})')
    rv_ag = find(r'22\.\s+Arbeitgeber[^\n]+\nJahreshinzurechnungsbetrag versicherung\s+([\d\.]+,\d{2})')
    if rv_ag == 0:
        rv_ag = find(r'22\.\s+Arbeitgeber[^\d\n]+\n[^\d\n]+\s+([\d\.]+,\d{2})')
    out['rv_ag'] = rv_ag
    out['rv_an'] = find(r'23\.\s+Arbeitnehmer[^\d]+Renten-?\n\s*versicherung\s+([\d\.]+,\d{2})')
    out['kv_an'] = find(r'25\.\s+Arbeitnehmerbeitr[^\d]+Kranken-?\n\s*versicherung\s+([\d\.]+,\d{2})')
    out['pv_an'] = find(r'26\.\s+Arbeitnehmerbeitr[^\d]+Pflege-?\n\s*versicherung\s+([\d\.]+,\d{2})')
    out['av_an'] = find(r'27\.\s+Arbeitnehmerbeitr[^\d]+Arbeitslosenver-?\n?\s*sicherung\s+([\d\.]+,\d{2})')
    out['verpflegungszuschuss_z20'] = find(r'Verpflegungszusch[^\d]+([\d\.]+,\d{2})')
    out['doppelhaus_z21']           = find(r'doppelter Haushalt[^\d]+([\d\.]+,\d{2})')
    return out


def _lsb_extract_via_claude(text, regex_hints=None, label='lsb-extract', extra_instruction=None):
    """Liefert die numerischen Felder via Claude. Wird IMMER aufgerufen
    (KI ist Source of Truth, Regex nur Cross-Check). Bei Diskrepanz mit
    regex_hints wird ein Re-Check ausgelöst.
    extra_instruction: optionaler zusätzlicher Anweisungsblock (z.B. Diskrepanz-Resolution)."""
    if not ANTHROPIC_KEY or not text:
        if not ANTHROPIC_KEY:
            print(f"[LSB-parser/{label}] ANTHROPIC_KEY fehlt → fallback auf Regex-only")
        return {}
    fields = ['brutto','lohnsteuer','soli','kirchensteuer_an','ag_fahrt_z17',
              'ag_fahrt_z18_pauschal','rv_ag','rv_an','kv_an','pv_an','av_an',
              'verpflegungszuschuss_z20','doppelhaus_z21']
    hint_block = ''
    if regex_hints:
        hint_lines = '\n'.join(f'  {k}: {v}' for k, v in regex_hints.items() if v)
        if hint_lines:
            hint_block = ('\nRegex-Vorschläge (zur Plausi-Prüfung — können falsch sein):\n'
                          + hint_lines + '\n')
    extra_block = f'\n{extra_instruction}\n' if extra_instruction else ''
    prompt = (
        "Du bekommst den Text einer deutschen Lohnsteuerbescheinigung. "
        "Lies sorgfältig und extrahiere die Beträge — gib NUR ein JSON-Objekt zurück, kein Erklärtext.\n\n"
        "Felder (alle in EUR als Zahl, 0 wenn nicht vorhanden):\n"
        '{\n'
        '  "brutto": <Bruttoarbeitslohn Zeile 3>,\n'
        '  "lohnsteuer": <einbehaltene Lohnsteuer Zeile 4>,\n'
        '  "soli": <Solidaritätszuschlag Zeile 5>,\n'
        '  "kirchensteuer_an": <Kirchensteuer AN Zeile 6/7>,\n'
        '  "ag_fahrt_z17": <Entfernungspauschale anzurechnen Zeile 17>,\n'
        '  "ag_fahrt_z18_pauschal": <15% pauschal Zeile 18>,\n'
        '  "rv_ag": <Arbeitgeber-RV Zeile 22a>,\n'
        '  "rv_an": <Arbeitnehmer-RV Zeile 23a>,\n'
        '  "kv_an": <KV-Arbeitnehmer Zeile 25>,\n'
        '  "pv_an": <PV-Arbeitnehmer Zeile 26>,\n'
        '  "av_an": <AV-Arbeitnehmer Zeile 27>,\n'
        '  "verpflegungszuschuss_z20": <stfrei Verpflegung Zeile 20>,\n'
        '  "doppelhaus_z21": <doppelter Haushalt Zeile 21>\n'
        '}\n'
        + hint_block + extra_block +
        "\nWichtig: deutsche Zahlen (1.234,56 = 1234.56 in JSON). Niemals Felder weglassen.\n\n"
        "PDF-Text:\n" + text[:10000]
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        resp = _claude_with_retry(client, 'claude-sonnet-4-5', 1500,
                                  [{'type': 'text', 'text': prompt}], label=label)
        ai_text = resp.content[0].text.strip()
        m = re.search(r'\{[\s\S]*\}', ai_text)
        if not m:
            print(f"[LSB-parser/{label}] kein JSON in Claude-Antwort: {ai_text[:200]}")
            return {}
        import json as _json
        data = _json.loads(m.group(0))
        out = {}
        for k in fields:
            v = data.get(k, 0)
            try: out[k] = float(v) if v else 0
            except: out[k] = 0
        return out
    except Exception as e:
        print(f"[LSB-parser/{label}] Claude fail: {e}")
        return {}


def parse_lohnsteuerbescheinigung(pdf_bytes_list):
    """
    Extrahiert ALLE steuerrelevanten Felder.
    Strategie: Regex (schnell) + Claude (immer) + Diskrepanz-Re-Check.
    KI ist Source of Truth — Regex nur Cross-Check + Notnagel bei API-Ausfall.
    """
    pdf_bytes_list = _bytes_list(pdf_bytes_list)
    result = {
        'brutto': 0, 'lohnsteuer': 0, 'soli': 0,
        'kirchensteuer_an': 0, 'kirchensteuer_eg': 0,
        'ag_fahrt_z17': 0, 'ag_fahrt_z18_pauschal': 0,
        'rv_an': 0, 'kv_an': 0, 'pv_an': 0, 'av_an': 0,
        'rv_ag': 0,
        'verpflegungszuschuss_z20': 0, 'doppelhaus_z21': 0,
        'identnr': '', 'geburtsdatum': '', 'personalnummer': '',
        'steuerklasse': '1', 'kinderfreibetraege': 0.0,
        'kirchensteuermerkmale': '',
        'arbeitgeber': 'Deutsche Lufthansa AG',
        'finanzamt': '', 'steuernummer_ag': '',
        'vorsorge_gesamt_an': 0,
        'rv_gesamt': 0,
    }

    for pdf_bytes in pdf_bytes_list:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = '\n'.join(p.extract_text() or '' for p in pdf.pages)

            print(f"[LSB-parser] PDF text len={len(text)}, head={text[:200].replace(chr(10),' | ')!r}")

            # ── 1) Regex-Extraktion (schnell, deterministisch) ──
            regex_vals = _lsb_extract_via_regex(text)
            print(f"[LSB-parser] Regex: brutto={regex_vals.get('brutto',0):.2f} "
                  f"z17={regex_vals.get('ag_fahrt_z17',0):.2f} "
                  f"lohnsteuer={regex_vals.get('lohnsteuer',0):.2f}")

            # ── 2) Claude-Extraktion (immer — Source of Truth) ──
            ai_vals = _lsb_extract_via_claude(text, regex_hints=regex_vals, label='lsb-primary')
            print(f"[LSB-parser] Claude: brutto={ai_vals.get('brutto',0):.2f} "
                  f"z17={ai_vals.get('ag_fahrt_z17',0):.2f} "
                  f"lohnsteuer={ai_vals.get('lohnsteuer',0):.2f}")

            # ── 3) Cross-Check + Diskrepanz-Resolution ──
            chosen = {}
            critical_fields = ['brutto', 'lohnsteuer', 'ag_fahrt_z17']
            disagreement = False
            for k in regex_vals.keys():
                rv = regex_vals.get(k, 0) or 0
                av = ai_vals.get(k, 0) or 0
                # Diskrepanz: beide Werte nicht 0 und differieren um >1 EUR oder >1%
                if rv > 0 and av > 0:
                    diff = abs(rv - av)
                    if diff > max(1.0, rv * 0.01):
                        if k in critical_fields:
                            disagreement = True
                            print(f"[LSB-parser] DISKREPANZ {k}: regex={rv:.2f} vs claude={av:.2f}")
                        chosen[k] = av  # KI gewinnt
                    else:
                        chosen[k] = av  # praktisch identisch, KI nehmen
                elif av > 0:
                    chosen[k] = av  # nur KI hat Wert
                elif rv > 0:
                    chosen[k] = rv  # nur Regex hat Wert (KI vermutlich offline)
                else:
                    chosen[k] = 0   # beide leer

            # Bei kritischer Diskrepanz: zweiter Claude-Call mit beiden Werten zur finalen Entscheidung
            if disagreement and ANTHROPIC_KEY:
                print(f"[LSB-parser] kritische Diskrepanz → Re-Check mit beiden Werten")
                resolution_text = (
                    "DISKREPANZ-RE-CHECK: Diese LSB wurde 2× gelesen mit unterschiedlichen Ergebnissen. "
                    "Lies JEDEN dieser Werte nochmal sorgfältig im PDF-Text nach. Achte auf Tausenderpunkte, "
                    "Kommas, korrekte Zeilen-Zuordnung. Gib das korrekte JSON zurück.\n"
                    "Vergleich der Quellen:\n" +
                    '\n'.join(f"  {k}: regex={regex_vals.get(k,0):.2f} vs claude={ai_vals.get(k,0):.2f}"
                              for k in critical_fields)
                )
                # Resolution als extra_instruction übergeben (nicht in text einbetten — wäre nach text[:10000] abgeschnitten)
                final_vals = _lsb_extract_via_claude(text, regex_hints=None,
                                                     label='lsb-recheck',
                                                     extra_instruction=resolution_text)
                if final_vals.get('brutto', 0) > 0:
                    print(f"[LSB-parser] Re-Check OK: brutto={final_vals.get('brutto',0):.2f}")
                    for k, v in final_vals.items():
                        if v > 0:
                            chosen[k] = v

            # Werte ins result-Dict übernehmen — bei mehreren LSB-PDFs (z.B. AG-Wechsel)
            # werden numerische Werte AKKUMULIERT, nicht überschrieben.
            for k in regex_vals.keys():
                v = chosen.get(k, 0) or 0
                if v > 0:
                    if isinstance(result.get(k), (int, float)):
                        result[k] = round((result[k] or 0) + v, 2)
                    else:
                        result[k] = v

            # ── 4) Persönliche Daten via Regex — nur setzen wenn noch nicht gesetzt
            # (bei Multi-LSB AG-Wechsel: erste LSB liefert die Stammdaten,
            #  zweite würde sonst überschreiben mit AG2-Werten)
            if result['brutto'] > 0:
                if not result.get('identnr'):
                    m_id = re.search(r'Identifikationsnummer:\s*(\d{11})', text)
                    if m_id: result['identnr'] = m_id.group(1)
                if not result.get('geburtsdatum'):
                    m_geb = re.search(r'Geburtsdatum:\s*(\d{2}\.\d{2}\.\d{4})', text)
                    if m_geb: result['geburtsdatum'] = m_geb.group(1)
                if not result.get('personalnummer'):
                    m_pnr = re.search(r'Personalnummer:\s*(\d+)', text)
                    if m_pnr: result['personalnummer'] = m_pnr.group(1)
                if result.get('steuerklasse') in (None, '', '1'):  # default ist '1'
                    m_sk = re.search(r'Steuerklasse/Faktor\s+(\d)', text)
                    if m_sk: result['steuerklasse'] = m_sk.group(1)
                if not result.get('kinderfreibetraege'):
                    m_kfb = re.search(r'Kinderfreibetr[^\d]+([\d,]+)', text)
                    if m_kfb:
                        try: result['kinderfreibetraege'] = float(m_kfb.group(1).replace(',','.'))
                        except: pass
                if not result.get('kirchensteuermerkmale'):
                    m_kst = re.search(r'Kirchensteuermerkmale\s+([\w\s/\-]+?)(?:\n|$)', text)
                    if m_kst: result['kirchensteuermerkmale'] = m_kst.group(1).strip()
                if not result.get('finanzamt'):
                    m_fa = re.search(r'Finanzamt[^\n]*\n([^\n]+)', text)
                    if m_fa: result['finanzamt'] = m_fa.group(1).strip()
                if not result.get('steuernummer_ag'):
                    m_stnr = re.search(r'Steuernummer:\s*([\d/]+)', text)
                    if m_stnr: result['steuernummer_ag'] = m_stnr.group(1)

                result['vorsorge_gesamt_an'] = round(
                    result['rv_an'] + result['kv_an'] +
                    result['pv_an'] + result['av_an'], 2)
                result['rv_gesamt'] = round(result['rv_an'] + result['rv_ag'], 2)
                print(f"[LSB-parser] FINAL brutto={result['brutto']:.2f} "
                      f"z17={result['ag_fahrt_z17']:.2f} disagreement={disagreement}")

        except Exception as e:
            print(f'[LSB-parser] PDF-Read fail: {e}')

    return result



def _claude_with_retry(client, model, max_tokens, content, max_retries=3, label='claude'):
    """Anthropic API mit exponential backoff. Schützt vor transienten Fehlern (429, 5xx, network).
    Liefert Response-Objekt zurück oder wirft nach max_retries die letzte Exception."""
    import time as _t
    last_err = None
    for attempt in range(max_retries):
        try:
            return client.messages.create(model=model, max_tokens=max_tokens,
                                          messages=[{'role': 'user', 'content': content}])
        except Exception as e:
            last_err = e
            err_str = str(e)
            # Retry bei: rate limit, 5xx, connection, timeout
            should_retry = any(s in err_str.lower() for s in ['429', '500', '502', '503', '504', 'timeout', 'connection', 'rate'])
            if not should_retry or attempt == max_retries - 1:
                print(f"[{label}] failed (attempt {attempt+1}/{max_retries}): {err_str[:200]}")
                raise
            wait = 2 ** attempt + 1
            print(f"[{label}] retry attempt {attempt+1}/{max_retries} in {wait}s — {err_str[:120]}")
            _t.sleep(wait)
    if last_err: raise last_err


def _claude_stream_with_retry(client, model, max_tokens, content, max_retries=3, label='claude-stream', prefill=None):
    """Wie _claude_with_retry, aber für Streaming-Calls. Liefert kompletten Text zurück.
    prefill (optional): String der als Anfang der Assistant-Antwort vorgegeben wird.
    Damit zwingt man Claude zu strukturierter Ausgabe (z.B. JSON) ohne Vorgeplänkel."""
    import time as _t
    last_err = None
    for attempt in range(max_retries):
        try:
            full_text = ''
            messages = [{'role': 'user', 'content': content}]
            if prefill:
                messages.append({'role': 'assistant', 'content': prefill})
            with client.messages.stream(model=model, max_tokens=max_tokens,
                                        messages=messages) as stream:
                for text in stream.text_stream:
                    full_text += text
            # Bei prefill: Antwort beginnt OHNE den prefill-String, also vorhängen
            if prefill:
                full_text = prefill + full_text
            return full_text.strip()
        except Exception as e:
            last_err = e
            err_str = str(e)
            should_retry = any(s in err_str.lower() for s in ['429', '500', '502', '503', '504', 'timeout', 'connection', 'rate'])
            if not should_retry or attempt == max_retries - 1:
                print(f"[{label}] failed (attempt {attempt+1}/{max_retries}): {err_str[:200]}")
                raise
            wait = 2 ** attempt + 1
            print(f"[{label}] retry attempt {attempt+1}/{max_retries} in {wait}s — {err_str[:120]}")
            _t.sleep(wait)
    if last_err: raise last_err


# ╔══════════════════════════════════════════════════════════════════╗
# ║ BMF-PAUSCHALEN — JÄHRLICHES REVIEW PFLICHT                       ║
# ║                                                                   ║
# ║ LAST-REVIEWED: 2026-05-09  (gültig für Steuerjahr 2025)         ║
# ║ NEXT-REVIEW:   2026-12-01  (für Steuerjahr 2026 finalisieren)   ║
# ║                                                                   ║
# ║ Quelle: BMF-Schreiben "Steuerliche Behandlung von Reisekosten"  ║
# ║   - Inland-Pauschalen: § 9 Abs. 4a EStG                         ║
# ║   - Auslands-Pauschalen: jährliches BMF-Schreiben (siehe        ║
# ║     bmf_data.py + IATA_TO_BMF)                                   ║
# ║   - Pendlerpauschale: § 9 Abs. 1 Nr. 4 EStG                     ║
# ║                                                                   ║
# ║ BEI NEUEM STEUERJAHR (Jan/Feb des Folgejahres):                 ║
# ║   1. BMF-Schreiben Reisekosten suchen (Bundesfinanzministerium)║
# ║   2. Inland: tagestrip_8h, an_abreise, voll_24h aktualisieren   ║
# ║   3. Auslandsspesen-Tabelle in bmf_data.py aktualisieren        ║
# ║   4. LAST-REVIEWED-Datum oben aktualisieren                      ║
# ║   5. Diesen Codeblock prüfen: Tests laufen lassen               ║
# ╚══════════════════════════════════════════════════════════════════╝
BMF_INLAND_BY_YEAR = {
    2023: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},
    2024: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},
    2025: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},
    2026: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},  # PROVISORISCH — Review Dez 2026
}

# Pendlerpauschale: 0,30€ km 1-20, 0,38€ ab km 21
# Stand 2025: gilt seit 2022, soll bis 2026 verlängert werden (Klimaschutz-Anhebung)
PENDLER_BY_YEAR = {
    2023: {'lt_20km': 0.30, 'gt_21km': 0.38},
    2024: {'lt_20km': 0.30, 'gt_21km': 0.38},
    2025: {'lt_20km': 0.30, 'gt_21km': 0.38},
    2026: {'lt_20km': 0.30, 'gt_21km': 0.38},
}

# Reinigungskosten-Pauschale (BFH-Verwaltungspraxis, ohne Beleg):
# 1,60€/Arbeitstag für Berufskleidung-Reinigung. Quelle: BFH VI R 56/91, Urteilspraxis.
REINIGUNG_PRO_TAG_BY_YEAR = {
    2023: 1.60, 2024: 1.60, 2025: 1.60, 2026: 1.60,
}

# Trinkgeld-Pauschale für Hotelnächte (Reisenebenkosten, § 9 Abs. 1 Nr. 5a EStG):
# 3,60€/Hotelnacht. Verwaltungspraxis, BFH-konform.
TRINKGELD_PRO_NACHT_BY_YEAR = {
    2023: 3.60, 2024: 3.60, 2025: 3.60, 2026: 3.60,
}


def _extract_homebase(base_str):
    """Extrahiert IATA-Code aus dem Form-Feld 'base' (z.B. 'Frankfurt (FRA)' → 'FRA').
    Fallback: FRA wenn nichts erkennbar."""
    if not base_str: return 'FRA'
    m = re.search(r'\(([A-Z]{3})\)', base_str)
    if m: return m.group(1)
    # Direkt 3-Letter-Code?
    m2 = re.match(r'^([A-Z]{3})$', base_str.strip().upper())
    if m2: return m2.group(1)
    # Stadtname → IATA-Mapping
    city_map = {
        'frankfurt':'FRA', 'münchen':'MUC', 'munich':'MUC',
        'hamburg':'HAM', 'düsseldorf':'DUS', 'duesseldorf':'DUS',
        'berlin':'BER', 'stuttgart':'STR', 'köln':'CGN', 'koeln':'CGN',
    }
    low = base_str.lower()
    for city, iata in city_map.items():
        if city in low: return iata
    return 'FRA'


def _parse_flugstunden_deterministic(flug_text, homebase='FRA'):
    """Liest LH Flugstunden-Übersicht LITERAL (kein Schätzen, kein Kalibrieren).

    Logik:
    1. Alle Zeilen nach Datum gruppieren (mehrere Zeilen pro Tag möglich, z.B. Same-Day-Tour)
    2. Pro Tag aktivität bestimmen:
       - FREI/URLAUB/KRANK/OF/LM → Frei-Tag
       - LH<num> A FRA → DEST = Tour-Start
       - LH<num> E ORIG → FRA = Tour-Ende
       - FL STRECKENEINSATZTAG = Layover-Tag (Hotelnacht)
       - SBY/RES/STANDBY/ONLINE/E-LEARNING = Home-Duty (Arbeitstag, kein Fahrtag)
    3. Counts:
       - fahrtag = jeder Tag mit "A FRA →" Pattern (= Tour-Beginn von Homebase)
       - arbeitstag = Tour-Tage (A, FL, E) + Home-Duty + Office-Duty
       - hotelnacht = jeder FL STRECKENEINSATZTAG
       - Same-day (A FRA + E FRA gleicher Tag) = 1 Fahrtag, 1 Arbeitstag, 0 Hotel
    """
    INLAND_IATA = {'FRA','MUC','HAM','DUS','BER','STR','CGN','NUE','LEJ',
                   'HAJ','HHN','BRE','DRS','ERF','NRN','FMO','LBC','TXL','PAD','SCN',
                   'XFW','RLG','SXF','TXF','MHG','FKB','FDH','DTM','FRO','HEI','KEL',
                   'BYU','EUM','OBF','BMK','RBM','EDLN','EDDF','EDDM','EDDH','EDDT',
                   'AOC','GWW','LHA','BFE'}

    # 1. Alle Zeilen mit Datum sammeln, gruppieren nach Datum
    days = {}  # 'DD.MM' → list of rest-strings (mehrere Einträge pro Tag möglich)
    for raw in flug_text.split('\n'):
        line = raw.strip()
        m = re.match(r'^(\d{2})\.(\d{2})\.\s+(.+)$', line)
        if not m: continue
        # Storno-Zeilen mit Trailing-X überspringen
        if re.search(r'\s+X\s*$', line):
            continue
        date_key = f"{m.group(1)}.{m.group(2)}"
        days.setdefault(date_key, []).append(m.group(3))

    # Pattern für jeden Aktivitätstyp (aplica auf ein "rest"-string)
    is_frei = lambda r: bool(re.search(
        r'(FREIER\s*TAG|FREI\b|URLAUB|KRANK|ARBEITSUNFAEHIG|UNBEZAHLT|MUTTERSCHUTZ'
        r'|ELTERNZEIT|^OF\s|^/-\s|^FR\s|^U\s|^K\s|NACHGEWAEHRUNG|KEIN\s+DIENST)', r, re.I))
    is_home_duty = lambda r: bool(re.search(
        r'\b(SBY|RES|RESERVE|STANDBY|ONLINE|E-LEARNING|ELEARNING|HOME|RB|RUFBEREITSCHAFT)\b', r, re.I))
    is_office_duty = lambda r: bool(re.search(
        r'\b(EM|EK|D4|EH|BRIEFING|SCHULUNG|SM|MEDICAL|SPRACHTEST)\b', r))
    is_layover = lambda r: bool(re.search(
        r'\bFL\b.*STRECKENEINSATZTAG|\bFL\b\s+STRECKEN', r, re.I)) or r.startswith('FL ')
    re_a_homebase = re.compile(rf'LH\d+\s+A\s+{homebase}\b.*?\b([A-Z]{{3}})\b')
    re_e_to_homebase = re.compile(rf'LH\d+\s+E\s+([A-Z]{{3}})\b.*?\b{homebase}\b')

    fahrtage = arbeitstage = hotel_naechte = frei_tage = 0
    z72_inland_days = 0  # Same-day Inland >8h
    z73_inland_days = 0  # Inland An- oder Abreisetag (mehrtägige Tour)
    unklare = []
    z72_candidates = []  # [{'date': '31.01', 'block_min': 180, 'dest': 'HAM'}]

    # Pattern für Block-Out-Block-In Zeiten in einer Zeile: "05:45-07:01"
    re_time_range = re.compile(r'(\d{2}):(\d{2})-(\d{2}):(\d{2})')

    def _block_span_minutes(rests_for_day):
        """Ermittelt Block-Out → Block-In Span in Minuten für alle Flüge eines Tages."""
        starts = []
        ends = []
        for r in rests_for_day:
            for m in re_time_range.finditer(r):
                sh, sm, eh, em = map(int, m.groups())
                start_min = sh*60 + sm
                end_min = eh*60 + em
                # Wenn Endzeit < Startzeit (Mitternacht-Crossing), 24h drauf
                if end_min < start_min:
                    end_min += 24*60
                starts.append(start_min)
                ends.append(end_min)
        if not starts:
            return None
        return max(ends) - min(starts)

    in_tour = False
    tour_inland_only = True  # Inland-Klassifikation des aktuellen Tour-Anfangs

    for date_key in sorted(days.keys(), key=lambda d: (int(d[3:5]), int(d[:2]))):
        rests = days[date_key]
        joined = ' || '.join(rests)

        # FREI hat höchste Priorität (auch wenn andere Marker da)
        if any(is_frei(r) for r in rests):
            frei_tage += 1
            in_tour = False
            continue

        # Layover-Tag (FL STRECKENEINSATZTAG)
        if any(is_layover(r) for r in rests):
            arbeitstage += 1
            hotel_naechte += 1
            in_tour = True
            continue

        # Tour-Aktivität an diesem Tag prüfen
        a_matches = []
        e_matches = []
        for r in rests:
            for m in re_a_homebase.finditer(r):
                a_matches.append(m.group(1))
            for m in re_e_to_homebase.finditer(r):
                e_matches.append(m.group(1))

        has_a = bool(a_matches)
        has_e = bool(e_matches)

        if has_a and has_e:
            # Zwei Sub-Cases:
            # A) Turnaround: in_tour war True → User landet morgens UND startet neue Tour abends
            #    → 1 Arbeitstag, KEIN zusätzlicher Fahrtag (User war am FRA, kein Heim-Pendel),
            #      2× Z73 bei Inland-Routen, in_tour bleibt True (neue Tour aktiv).
            # B) Same-Day-Tour: in_tour war False → A FRA → DEST → E DEST → FRA am gleichen Tag
            #    → 1 Arbeitstag, 1 Fahrtag, ggf. Z72.
            arbeitstage += 1
            origin = e_matches[0]
            ziel   = a_matches[0]
            if in_tour:
                # Turnaround
                if origin in INLAND_IATA:
                    z73_inland_days += 1  # alte Tour Abreise
                if ziel in INLAND_IATA:
                    z73_inland_days += 1  # neue Tour Anreise
                tour_inland_only = (ziel in INLAND_IATA)
                # in_tour bleibt True
            else:
                # Echte Same-Day-Tour
                fahrtage += 1
                in_tour = False
                if ziel in INLAND_IATA:
                    z72_inland_days += 1
                    block_min = _block_span_minutes(rests)
                    if block_min is not None:
                        z72_candidates.append({
                            'date': date_key,
                            'block_min': block_min,
                            'dest': ziel,
                        })
                # Auslands-Same-Day-Tour: kein Z72 (das wäre Z76, kommt aus SE)
            continue

        if has_a:
            # Tour-Start
            arbeitstage += 1
            if not in_tour:
                fahrtage += 1
                in_tour = True
            ziel = a_matches[0]
            tour_inland_only = (ziel in INLAND_IATA)
            if tour_inland_only:
                z73_inland_days += 1  # Anreise-Tag Inland
            continue

        if has_e:
            # Tour-Ende
            arbeitstage += 1
            in_tour = False
            origin = e_matches[0]
            if origin in INLAND_IATA:
                z73_inland_days += 1  # Abreise-Tag von Inland
            continue

        # Home-Duty (Standby/Reserve)
        if any(is_home_duty(r) for r in rests):
            arbeitstage += 1
            continue

        # Office-Duty (Briefing, Schulung etc)
        if any(is_office_duty(r) for r in rests):
            arbeitstage += 1
            if not in_tour:
                fahrtage += 1
            continue

        unklare.append(f"{date_key}: {joined[:120]}")

    return {
        'fahrtage':         fahrtage,
        'arbeitstage':      arbeitstage,
        'hotel_naechte':    hotel_naechte,
        'z72_inland_days':  z72_inland_days,
        'z73_inland_days':  z73_inland_days,
        'z72_candidates':   z72_candidates,    # pro-Tag Block-Time für Z72-Berechnung
        'frei_tage':        frei_tage,
        'unklare_tage':     unklare,
    }


def _parse_se_pdf_xpos(pdf_bytes_list, year=2025):
    """SE-Parser mit pdfplumber x-Position. 100% deterministisch — liest die
    Spalten Datum/Ab/An/Spesen/Ort/Zwölftel/stfrei/Ort literal anhand der x-Koordinaten.
    Liefert Tag-Counts + literal-Summen.

    Wenn LH keinen stfrei-Wert ausweist (kürzere Touren ohne LH-Spesen-Anspruch):
    BMF-Pauschale für das Land aus offizieller Tabelle anwenden (rechtlich legitim
    nach §9 EStG).
    """
    from collections import defaultdict
    import pdfplumber, io as _io

    INLAND = {'FRA','HAM','MUC','BER','DUS','STR','NUE','CGN','LEJ','HAJ',
              'HHN','BRE','DRS','ERF','NRN','FMO','LBC','TXL','PAD','SCN','XFW','RLG',
              'FDH','DTM','SXF','TXF','MHG','FKB'}

    # Spalten-x-Bereiche (validiert gegen LH SE-Format)
    COL = {
        'datum':      (60, 110),
        'ab':         (110, 140),
        'an':         (140, 175),
        'spesen_eur': (175, 235),
        'spesen_ort': (235, 265),
        'zwf':        (265, 310),
        'stfrei_eur': (310, 350),
        'stfrei_ort': (350, 380),
        'steuer':     (380, 420),
        'werbko':     (420, 460),
        'storno':     (490, 540),
    }

    def col_of(x):
        for c, (a, b) in COL.items():
            if a <= x < b: return c
        return None

    def num(s):
        if not s: return None
        s = s.replace('-','').strip()
        try: return float(s.replace('.','').replace(',','.'))
        except: return None

    rows = []
    for pdf_bytes in _bytes_list(pdf_bytes_list):
        try:
            with pdfplumber.open(_io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    words = page.extract_words(use_text_flow=False, x_tolerance=2)
                    by_y = defaultdict(list)
                    for w in words:
                        by_y[round(w['top'] / 6) * 6].append(w)
                    for y in sorted(by_y.keys()):
                        rw = sorted(by_y[y], key=lambda x: x['x0'])
                        if not rw: continue
                        first = rw[0]['text']
                        if not re.match(r'^\d{2}\.\d{2}\.\d{4}$', first): continue
                        row = {}
                        for w in rw:
                            c = col_of(w['x0'])
                            if c: row.setdefault(c, []).append(w['text'])
                        # Storno-Markierung
                        st = ''.join(row.get('storno', []))
                        if 'X' in st: continue
                        rows.append(row)
        except Exception as e:
            print(f"[SE-xpos] page parse fail: {e}")

    z72_tage = z73_tage = z74_tage = 0
    z72_eur = z73_eur = z74_eur = z76_eur = 0.0
    z76_eur_bmf_fallback = 0.0   # Anteil aus BMF-Pauschalen (LH zahlt nichts)
    bmf_fallback_count = 0
    z77_werbko = 0.0
    fahrtage_inland = 0
    fahrtage_ausland = 0
    arbeitstage = len(rows)
    hotelnaechte = 0
    unklar = []
    # Tour-Tracking: pro Row protokollieren wir (datum, kategorie, eur, ist_inland_anreise).
    # Nach dem Loop machen wir einen 2. Pass: Inland-An-/Abreise-Tage, die direkter Nachbar
    # einer Auslands-Zeile sind, werden zu Z76 reklassifiziert (= Auslandstour-Anreise/Abreise).
    row_classifications = []  # list of dicts: {'datum': date, 'kat': 'z72'|'z73'|'z74'|'z76', 'eur': float, 'reklassifizierbar': bool}

    def bmf_24h(ort):
        v = bmf_lookup(ort, year)
        return v[0] if v else None
    def bmf_an(ort):
        v = bmf_lookup(ort, year)
        return v[1] if v else None

    def _datum(r):
        try:
            d = r.get('datum', [''])
            ds = d[0] if isinstance(d, list) else d
            return ds  # 'DD.MM.YYYY' string is good enough for sorting
        except: return ''

    for r in rows:
        ab = (r.get('ab') or [None])[0]
        an = (r.get('an') or [None])[0]
        sf_eur = num((r.get('stfrei_eur') or [''])[0])
        sf_ort = (r.get('stfrei_ort') or [None])[0]
        sp_ort = (r.get('spesen_ort') or [None])[0]
        wk = num((r.get('werbko') or [''])[0])
        if wk: z77_werbko += wk
        kat_ort = sf_ort or sp_ort
        is_inland = kat_ort in INLAND if kat_ort else False
        has_ab = ab is not None
        has_an = an is not None

        datum = _datum(r)
        eur_used = sf_eur if sf_eur else (14.0 if is_inland else 0.0)
        kat = None  # 'z72', 'z73', 'z74', 'z76'
        # Reklassifizierbar = Inland-An-/Abreise (kein Tagestrip, kein 24h)
        # → kann später zu Z76 werden wenn Nachbar einer Auslandszeile
        reklass = False

        if has_ab and has_an:
            # Same-Day-Tour
            if is_inland:
                z72_tage += 1
                z72_eur += sf_eur if sf_eur else 14.0
                fahrtage_inland += 1
                kat = 'z72'
            else:
                if sf_eur:
                    z76_eur += sf_eur
                fahrtage_ausland += 1
                kat = 'z76'
        elif has_ab and not has_an:
            # Anreise-Tag
            if is_inland:
                z73_tage += 1
                z73_eur += sf_eur if sf_eur else 14.0
                fahrtage_inland += 1
                kat = 'z73'
                reklass = True
            else:
                if sf_eur:
                    z76_eur += sf_eur
                else:
                    fb = bmf_an(kat_ort)
                    if fb:
                        z76_eur += fb
                        z76_eur_bmf_fallback += fb
                        bmf_fallback_count += 1
                    else:
                        unklar.append(r)
                fahrtage_ausland += 1
                kat = 'z76'
            hotelnaechte += 1
        elif not has_ab and has_an:
            # Abreise-Tag
            if is_inland:
                z73_tage += 1
                z73_eur += sf_eur if sf_eur else 14.0
                kat = 'z73'
                reklass = True
            else:
                if sf_eur:
                    z76_eur += sf_eur
                else:
                    fb = bmf_an(kat_ort)
                    if fb:
                        z76_eur += fb
                        z76_eur_bmf_fallback += fb
                        bmf_fallback_count += 1
                    else:
                        unklar.append(r)
                kat = 'z76'
        else:
            # Voll-Tag (24h auswärts)
            if is_inland:
                z74_tage += 1
                z74_eur += sf_eur if sf_eur else 28.0
                kat = 'z74'
            else:
                if sf_eur:
                    z76_eur += sf_eur
                else:
                    fb = bmf_24h(kat_ort)
                    if fb:
                        z76_eur += fb
                        z76_eur_bmf_fallback += fb
                        bmf_fallback_count += 1
                    else:
                        unklar.append(r)
                kat = 'z76'
            hotelnaechte += 1

        if kat:
            row_classifications.append({
                'datum': datum,
                'kat':   kat,
                'eur':   sf_eur if sf_eur else (14.0 if kat in ('z72','z73') else 28.0 if kat == 'z74' else 0.0),
                'reklass': reklass,
                'kat_ort': kat_ort,
            })

    # ── TOUR-AWARE RE-KLASSIFIZIERUNG DEAKTIVIERT ──────────────
    # Mein vorheriger Fix war zu aggressiv: hat ALLE Z73-Tage in Auslandstour-
    # Nachbarschaft zu Z76 reklassifiziert UND mit Auslands-Pauschalen aufgewertet.
    # Steuerberater-Praxis (branchenüblicher Steuerberater-Methode) ist konservativer:
    # Inland-Anteile (FRA stfrei) bleiben Z73 mit 14€ — Z76 ist nur was LH
    # explizit als Auslandsspesen markiert. Sicherer beim Finanzamt.
    # Nur die deterministische Klassifikation aus der Loop bleibt.
    pass

    return {
        'z72_tage': z72_tage, 'z72_eur': round(z72_eur, 2),
        'z73_tage': z73_tage, 'z73_eur': round(z73_eur, 2),
        'z74_tage': z74_tage, 'z74_eur': round(z74_eur, 2),
        'z76_eur':  round(z76_eur, 2),
        'z76_eur_bmf_fallback': round(z76_eur_bmf_fallback, 2),
        'bmf_fallback_count': bmf_fallback_count,
        'z77_werbko': round(z77_werbko, 2),
        'fahrtage':    fahrtage_inland + fahrtage_ausland,
        'arbeitstage': arbeitstage,
        'hotelnaechte': hotelnaechte,
        'unklare_zeilen': [' '.join(' '.join(v) for v in r.values()) for r in unklar],
    }


def _parse_se_lines_deterministic(all_se_text):
    """Liest SE Zeile für Zeile, kategorisiert nach stfrei-Ort + AB/AN-Pattern.
    Liefert literal-Werte aus dem Dokument — keine BMF-Tabellen, keine Schätzungen.
    Edge-Cases (z.B. Ausland ohne stfrei-Wert) werden als 'unklare_zeilen' für Claude markiert.
    """
    INLAND = {'FRA','HAM','MUC','BER','DUS','STR','NUE','CGN','LEJ','HAJ',
              'HHN','BRE','DRS','ERF','NRN','FMO','LBC','TXL','PAD','SCN','XFW','RLG',
              'FDH','DTM','SXF','TXF','MHG','FKB'}
    z72_count = z73_count = z74_count = 0
    z72_eur = z73_eur = z74_eur = z76_eur = 0.0
    unklare = []

    # Number-Pattern toleriert Thousands-Separator: "1.234,56" oder "234,56" oder "12,60"
    NUM_PATTERN = re.compile(r'^[\d]{1,3}(?:\.\d{3})*,\d{2}$|^\d+,\d{2}$')

    for line in all_se_text.split('\n'):
        line = line.strip()
        if not line: continue
        # Storno-Erkennung präziser: einzelnes "X" am Zeilenende oder " X " in Spalten-Position
        if re.search(r'\s+X\s*$', line) or '  X  ' in line: continue
        if not re.match(r'^\d{2}\.\d{2}\.\d{4}', line): continue   # nur Datums-Zeilen

        parts = line.split()
        idx = 1
        ab = an = None
        if idx<len(parts) and re.match(r'^\d{2}:\d{2}$', parts[idx]): ab = parts[idx]; idx += 1
        if idx<len(parts) and re.match(r'^\d{2}:\d{2}$', parts[idx]): an = parts[idx]; idx += 1
        if idx >= len(parts): continue
        # Spesenanspruch (überspringen — wir interessieren uns für stfrei)
        if not NUM_PATTERN.match(parts[idx]):
            unklare.append(line); continue
        idx += 1
        if idx >= len(parts): continue
        ort = parts[idx]; idx += 1
        if idx >= len(parts): continue
        try: zwf = int(parts[idx]); idx += 1
        except:
            unklare.append(line); continue

        # stfrei optional, stfrei-Ort optional
        sf_val = None
        sf_ort = None
        if idx<len(parts) and NUM_PATTERN.match(parts[idx]):
            try: sf_val = float(parts[idx].replace('.','').replace(',','.'))
            except: pass
            idx += 1
        if idx<len(parts) and re.match(r'^[A-Z]{2,4}$', parts[idx]):
            sf_ort = parts[idx]; idx += 1

        # Wenn weder stfrei-Wert noch stfrei-Ort, Zeile nicht eindeutig
        if sf_val is None and sf_ort is None:
            unklare.append(line); continue

        # Kategorie aus stfrei-Ort (Priorität) oder ort
        kategorie_ort = sf_ort if sf_ort else ort
        is_inland = kategorie_ort in INLAND
        has_ab = ab is not None
        has_an = an is not None

        if is_inland:
            # Inland-Klassifizierung nach AB/AN-Pattern:
            # - AB+AN beide vorhanden = Tagestrip Inland (Z72, 14€)
            # - keine Zeiten + 12 zwölftel = 24h Inland (Z74, 28€)
            # - sonst (nur AB oder nur AN) = An-/Abreise mit Übernachtung (Z73, 14€)
            if has_ab and has_an:
                z72_count += 1
                z72_eur += sf_val if sf_val else 14.0
            elif zwf == 12 and not has_ab and not has_an:
                z74_count += 1
                z74_eur += sf_val if sf_val else 28.0
            else:
                z73_count += 1
                z73_eur += sf_val if sf_val else 14.0
        else:
            # Ausland — literal stfrei-Wert addieren
            if sf_val is not None:
                z76_eur += sf_val
            else:
                # Ausland ohne stfrei-Wert → BMF-Lookup nötig → Claude
                unklare.append(line)

    return {
        'z72_tage': z72_count, 'z72_eur': round(z72_eur, 2),
        'z73_tage': z73_count, 'z73_eur': round(z73_eur, 2),
        'z74_tage': z74_count, 'z74_eur': round(z74_eur, 2),
        'z76_eur':  round(z76_eur, 2),
        'unklare_zeilen': unklare,
    }


def parse_streckeneinsatz_mit_ki(pdf_bytes_list, year=2025):
    """
    Liest Lufthansa Streckeneinsatz-Abrechnungen.

    VERIFIZIERTE FORMELN (gegen Referenz-Auswertung getestet):

    Z77 (steuerfrei gesamt):
        Pro Abrechnung: Z77 = Gesamt - letzter_Wert der "Summe:"-Zeile
        "Summe: G C2 C3" → Z77 = G - C3  (3 Spalten)
        "Summe: G C2"    → Z77 = G - C2  (2 Spalten)
        Summe über alle Abrechnungen = exakt Z77

    Z73 (An-/Abreisetage):
        Zeilen mit Muster "14,00 FRA" = Anreisetage von Homebase FRA
        Z73 = Anzahl × 14€

    Diese Werte werden per Regex berechnet (100% zuverlässig).
    Claude berechnet Z76 (VMA Ausland) zusätzlich aus den Einzelzeilen.
    """
    pdf_bytes_list = _bytes_list(pdf_bytes_list)
    if not pdf_bytes_list:
        return None

    # ── SCHRITT 1: REGEX — Z77 + Z73 + Abrechnungen (100% VERLÄSSLICH) ──
    abrechnungen = []
    z73_tage = 0
    flugmonate = set()  # echte Flugmonate aus den Flug-Zeilen (eine SE kann 2 Monate enthalten)

    for pdf_idx, pdf_bytes in enumerate(pdf_bytes_list):
        pdf_label = f"SE#{pdf_idx+1}"
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                page_count = len(pdf.pages)
                print(f"[SE-parser] {pdf_label}: {page_count} Seiten")
                for page_idx, page in enumerate(pdf.pages):
                    text = page.extract_text() or ''
                    if 'Streckeneinsatz' not in text and 'stfrei' not in text:
                        print(f"[SE-parser] {pdf_label} p{page_idx+1}: SKIP (kein 'Streckeneinsatz'/'stfrei' im Text, len={len(text)})")
                        continue

                    # Echte Flugmonate aus DD.MM.YYYY-Zeilenanfängen extrahieren
                    for fm in re.finditer(r'(?m)^(\d{2})\.(\d{2})\.(\d{4})\b', text):
                        try:
                            mn = int(fm.group(2))
                            if 1 <= mn <= 12:
                                flugmonate.add(mn)
                        except: pass

                    # Erstellungsdatum → Monatsbezeichnung
                    m_erst = re.search(r'Erstellt\s+(\d{2})\.(\d{2})\.(\d{4})', text)
                    if not m_erst:
                        print(f"[SE-parser] {pdf_label} p{page_idx+1}: SKIP (kein 'Erstellt'-Datum)")
                        continue
                    erstellt = f"{m_erst.group(1)}.{m_erst.group(2)}.{m_erst.group(3)}"
                    try:
                        mo_nr = int(m_erst.group(2))
                        mo_name = __import__('datetime').date(2025, mo_nr, 1).strftime('%B')
                    except:
                        mo_nr = 0
                        mo_name = f"Monat {m_erst.group(2)}"

                    # Summen-Zeile → Z77 dieser Abrechnung
                    # FORMEL: Z77 = Gesamt - letzter_Wert (Steuer/Steuerpflichtig)
                    m_sum = re.search(
                        r'Summe:\s+([\d\.]+,\d{2})\s+([\d\.]+,\d{2})(?:\s+([\d\.]+,\d{2}))?',
                        text)
                    if not m_sum:
                        # Fallback-Patterns: Summe ohne Doppelpunkt, oder mit Tab/multi-space
                        m_sum = re.search(
                            r'Summe[:\s]+([\d\.]+,\d{2})\s+([\d\.]+,\d{2})(?:\s+([\d\.]+,\d{2}))?',
                            text)
                    if not m_sum:
                        # Allerletzter Fallback: irgendwo am Seitenende stehen drei EUR-Beträge
                        m_sum = re.search(
                            r'(\d{2,5}[\.,]\d{2})\s+(\d{1,5}[\.,]\d{2})\s+(\d{1,5}[\.,]\d{2})\s*$',
                            text.strip(), re.M)
                    if not m_sum:
                        print(f"[SE-parser] {pdf_label} p{page_idx+1}: SKIP (keine 'Summe:'-Zeile gefunden — text-len={len(text)}, last200={text[-200:].replace(chr(10),' | ')!r})")
                        continue

                    def f(s): return float(s.replace('.','').replace(',','.')) if s else 0.0
                    g = f(m_sum.group(1))
                    c2 = f(m_sum.group(2))
                    c3 = f(m_sum.group(3))
                    steuer = c3 if c3 > 0 else c2      # letzter vorhandener Wert = Steuer
                    # Sanity-Check: Steuer kann nie größer als Gesamt sein → ggf. Spalten vertauscht
                    if steuer > g and c2 > 0 and c2 < g:
                        steuer = c2  # fallback: zweiter Wert ist Steuer, dritter wäre dann Steuerfrei
                    z77_page = round(max(0, g - steuer), 2)

                    # Z73 Anreisetage dieser Seite: "14,00 FRA" in Einzelzeilen
                    z73_page = text.count('14,00 FRA')

                    abrechnungen.append({
                        'erstellt':       erstellt,
                        'bezeichnung':    mo_name,
                        'monat':          mo_nr,           # Erstellt-Monat (zur Info, nicht zuverlässig)
                        'gesamt':         g,
                        'steuerfrei':     z77_page,        # Z77-Anteil
                        'steuerpflichtig': steuer,
                        'z73_tage':       z73_page,
                    })
                    z73_tage += z73_page

        except Exception as e:
            print(f'SE Regex error: {e}')

    # ── KI-VALIDIERUNG: Claude verifiziert die Abrechnungen + Z77-Summen ──
    # Auch wenn keine Abrechnungen via Regex gefunden wurden, fragt Claude
    # (Format-Änderungen werden so abgefangen).
    claude_abrechnungen = []
    claude_tage_klass = None  # Tour-aware Tag-Klassifikation von Claude (Source of Truth)
    if ANTHROPIC_KEY:
        try:
            all_text = ''
            for pdf_bytes in pdf_bytes_list:
                try:
                    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                        all_text += '\n=== PDF ===\n' + '\n'.join(p.extract_text() or '' for p in pdf.pages)
                except: pass
            if all_text and len(all_text) > 100:
                hint_block = ''
                if abrechnungen:
                    hint_lines = '\n'.join(
                        f"  - Erstellt {a['erstellt']}: gesamt={a['gesamt']:.2f} steuerfrei(Z77)={a['steuerfrei']:.2f} steuer={a['steuerpflichtig']:.2f}"
                        for a in abrechnungen)
                    hint_block = f"\nRegex hat folgende Abrechnungen gefunden (kann unvollständig/falsch sein):\n{hint_lines}\n"
                prompt = (
                    "Du liest Lufthansa Streckeneinsatz-Abrechnungen im Werbungskosten-Kontext. "
                    "Du klassifizierst die Tage steuerlich richtig nach §9 EStG.\n\n"
                    "Pro Seite gibt es eine 'Summe:'-Zeile (Gesamt/Steuer/Stfrei). "
                    "Z77 = stfrei-Anteil = Gesamt - letzter_Wert.\n\n"
                    "KLASSIFIKATION DER EINZELNEN TAGE — KONSERVATIV (branchenüblicher Steuerberater-Methode):\n"
                    "- stfrei-Ort = Inland (FRA/MUC/HAM/...): zähle als Z72/Z73/Z74 mit dem stfrei-Wert\n"
                    "  (auch wenn der Tag teil einer Auslandstour ist — bleibt Z73 mit 14€)\n"
                    "- stfrei-Ort = Ausland (GRU/JFK/SEL/...): zähle als Z76 mit dem stfrei-Wert\n"
                    "- Inland-Anteile von Auslandstouren NICHT künstlich auf Auslandspauschale aufwerten\n"
                    "- Konservativ ist sicherer beim Finanzamt\n\n"
                    "Extrahiere ALLE Abrechnungen + EINE TAGES-ZUSAMMENFASSUNG. Gib NUR JSON zurück, kein Erklärtext.\n\n"
                    "Format:\n"
                    '{\n'
                    '  "abrechnungen": [\n'
                    '    {"erstellt": "DD.MM.YYYY", "monat": <1-12>, "gesamt": <EUR>, "steuerfrei": <EUR>, "steuerpflichtig": <EUR>}\n'
                    '  ],\n'
                    '  "flugmonate": [<int>, ...],\n'
                    '  "tage_klassifiziert": {\n'
                    '    "z72_tage": <Anzahl Tagestrips Inland >8h>,\n'
                    '    "z72_eur":  <Σ Pauschalen für Z72>,\n'
                    '    "z73_tage": <Anzahl ECHTE Inland-An-/Abreisetage (Inland-Tour mit Inland-Hotel)>,\n'
                    '    "z73_eur":  <Σ Pauschalen für Z73>,\n'
                    '    "z74_tage": <Anzahl 24h Inland>,\n'
                    '    "z74_eur":  <Σ Pauschalen für Z74>,\n'
                    '    "z76_eur":  <Σ Auslands-Pauschalen INKL. der Auslandstour-Anreise/Abreise-Tage mit Inland-stfrei-Ort>\n'
                    '  }\n'
                    '}\n\n'
                    "Regeln:\n"
                    "- 'erstellt' = Erstellt-Datum (DD.MM.YYYY)\n"
                    "- 'monat' = Monat der Erstellung\n"
                    "- 'gesamt' = erste Zahl der Summe-Zeile\n"
                    "- 'steuerfrei' = letzte Zahl der Summe-Zeile (Z77)\n"
                    "- 'flugmonate' = alle Monate mit Flügen (1-12)\n"
                    "- 'tage_klassifiziert' = TOUR-AWARE Klassifikation aller Tage:\n"
                    "  → Z72: Tag mit AB+AN gleicher Tag, stfrei-Ort=Inland, KEIN Übernachtungs-Kontext\n"
                    "  → Z73: Inland-Anreise/Abreise NUR wenn Tour insgesamt im Inland (z.B. Schulung in MUC mit Hotel)\n"
                    "  → Z74: Inland 24h ohne Ab/An-Zeiten, zwf=12 (selten)\n"
                    "  → Z76: ALLE Auslandstage INKL. Inland-Anreise/Abreise-Tage einer Auslandstour\n"
                    "        (use BMF-Anreise-Pauschale des Ausland-Ziels für die Inland-Anteile)\n"
                    "- BMF-Auslands-Anreise-Pauschalen: 80% des 24h-Satzes für das Land. "
                    "  Bei stfrei-Wert in der Zeile: nimm den, sonst BMF-Tabelle.\n"
                    "- Deutsche Zahlen (1.234,56 → 1234.56 in JSON)\n"
                    + hint_block +
                    "\nSE-Text:\n" + all_text[:50000]
                )
                client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
                resp = _claude_with_retry(client, 'claude-sonnet-4-5', 6000,
                                          [{'type': 'text', 'text': prompt}], label='se-verify')
                ai_text = resp.content[0].text.strip()
                m = re.search(r'\{[\s\S]*\}', ai_text)
                if m:
                    import json as _json
                    data = _json.loads(m.group(0))
                    for a in data.get('abrechnungen', []):
                        try:
                            claude_abrechnungen.append({
                                'erstellt':       str(a.get('erstellt', '')),
                                'bezeichnung':    str(a.get('erstellt', '')),
                                'monat':          int(a.get('monat', 0) or 0),
                                'gesamt':         float(a.get('gesamt', 0) or 0),
                                'steuerfrei':     float(a.get('steuerfrei', 0) or 0),
                                'steuerpflichtig': float(a.get('steuerpflichtig', 0) or 0),
                                'z73_tage':       0,
                            })
                        except Exception as ie:
                            print(f"[SE-claude] entry parse fail: {ie}")
                    # Flugmonate aus Claude addieren
                    for mn in data.get('flugmonate', []):
                        try:
                            mi = int(mn)
                            if 1 <= mi <= 12:
                                flugmonate.add(mi)
                        except: pass
                    # Tour-aware Tag-Klassifikation von Claude (KI-Source-of-Truth)
                    tk = data.get('tage_klassifiziert') or {}
                    if tk:
                        try:
                            claude_tage_klass = {
                                'z72_tage': int(tk.get('z72_tage', 0) or 0),
                                'z72_eur':  float(tk.get('z72_eur', 0) or 0),
                                'z73_tage': int(tk.get('z73_tage', 0) or 0),
                                'z73_eur':  float(tk.get('z73_eur', 0) or 0),
                                'z74_tage': int(tk.get('z74_tage', 0) or 0),
                                'z74_eur':  float(tk.get('z74_eur', 0) or 0),
                                'z76_eur':  float(tk.get('z76_eur', 0) or 0),
                            }
                            print(f"[SE-claude] Tour-aware: Z72={claude_tage_klass['z72_tage']}T/{claude_tage_klass['z72_eur']:.2f}€  "
                                  f"Z73={claude_tage_klass['z73_tage']}T/{claude_tage_klass['z73_eur']:.2f}€  "
                                  f"Z74={claude_tage_klass['z74_tage']}T/{claude_tage_klass['z74_eur']:.2f}€  "
                                  f"Z76={claude_tage_klass['z76_eur']:.2f}€")
                        except Exception as _te:
                            print(f"[SE-claude] tage_klassifiziert parse fail: {_te}")
                            claude_tage_klass = None
                    else:
                        claude_tage_klass = None
                    print(f"[SE-claude] {len(claude_abrechnungen)} Abrechnungen, "
                          f"Z77-Total={sum(a['steuerfrei'] for a in claude_abrechnungen):.2f}€")
        except Exception as e:
            print(f"[SE-claude] verification fail: {e}")
            claude_tage_klass = None
    else:
        claude_tage_klass = None

    # ── Reconciliation: KI gewinnt wenn sie mehr/andere Werte hat ──
    regex_z77 = round(sum(a['steuerfrei'] for a in abrechnungen), 2)
    claude_z77 = round(sum(a['steuerfrei'] for a in claude_abrechnungen), 2)
    # Z73 robust mappen: per Erstellt-Datum (eindeutig), nicht per monat (kann kollidieren)
    regex_z73_by_erstellt = {a.get('erstellt', ''): a.get('z73_tage', 0) for a in abrechnungen}
    if claude_abrechnungen and (
        len(claude_abrechnungen) > len(abrechnungen) or
        abs(claude_z77 - regex_z77) > 5.0  # mehr als 5€ Diff
    ):
        # Z73-Tage von Regex erhalten — primär per Erstellt-Datum (eindeutig),
        # fallback per Monat (für Claude-Einträge ohne erstellt-Match)
        regex_z73_by_month = {a.get('monat', 0): a.get('z73_tage', 0) for a in abrechnungen}
        for ca in claude_abrechnungen:
            erstellt = ca.get('erstellt', '')
            if erstellt and erstellt in regex_z73_by_erstellt:
                ca['z73_tage'] = regex_z73_by_erstellt[erstellt]
            else:
                ca['z73_tage'] = regex_z73_by_month.get(ca.get('monat', 0), 0)
        # Fehlende Z73 (kein Match) per Regex auf Gesamt-Text rekonstruieren
        # (stellt sicher dass Z73 nicht verloren geht wenn Claude komplett neue Abrechnungen findet)
        if all(ca.get('z73_tage', 0) == 0 for ca in claude_abrechnungen) and abrechnungen:
            total_z73 = sum(a.get('z73_tage', 0) for a in abrechnungen)
            if total_z73 > 0 and claude_abrechnungen:
                # Verteile gleichmäßig auf Claude-Abrechnungen die was haben
                per_abr = total_z73 // len(claude_abrechnungen)
                rest = total_z73 % len(claude_abrechnungen)
                for i, ca in enumerate(claude_abrechnungen):
                    ca['z73_tage'] = per_abr + (1 if i < rest else 0)
                print(f"[SE] Z73-Tage ({total_z73}T) auf {len(claude_abrechnungen)} Claude-Abrechnungen verteilt")
        print(f"[SE] KI gewinnt: regex={len(abrechnungen)} abr / Z77={regex_z77:.2f}€ "
              f"vs claude={len(claude_abrechnungen)} abr / Z77={claude_z77:.2f}€")
        abrechnungen = claude_abrechnungen
    elif claude_abrechnungen:
        print(f"[SE] regex+claude einig: {len(abrechnungen)} Abrechnungen, Z77={regex_z77:.2f}€")

    if not abrechnungen:
        return None

    z77_total = round(sum(a['steuerfrei'] for a in abrechnungen), 2)

    # ── DETERMINISTISCHES LESEN aller SE-Zeilen via x-Position ──
    # Primär: pdfplumber x-Position Parser (100% deterministisch via Spalten-Koordinaten)
    # Fallback: Text-Regex Parser falls x-Position-Parser keine Rows findet
    se_det = _parse_se_pdf_xpos(pdf_bytes_list, year=year)
    if se_det['arbeitstage'] == 0:
        # Fallback Text-Regex
        all_se_text = ''
        for pdf_bytes in pdf_bytes_list:
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    all_se_text += '\n'.join(p.extract_text() or '' for p in pdf.pages) + '\n'
            except: pass
        se_det = _parse_se_lines_deterministic(all_se_text)
        # arbeitstage/fahrtage/hotelnaechte fehlen im Text-Regex Output → mit 0 füllen
        se_det.setdefault('arbeitstage', 0)
        se_det.setdefault('fahrtage', 0)
        se_det.setdefault('hotelnaechte', 0)
    print(f"SE deterministisch: Z77={z77_total:.2f}€  Z72={se_det['z72_tage']}T/{se_det['z72_eur']}€  Z73={se_det['z73_tage']}T/{se_det['z73_eur']}€  Z74={se_det['z74_tage']}T/{se_det['z74_eur']}€  Z76={se_det['z76_eur']}€  arbeitstage={se_det.get('arbeitstage',0)} hotel={se_det.get('hotelnaechte',0)} fahr={se_det.get('fahrtage',0)} unklar={len(se_det['unklare_zeilen'])} Zeilen")

    # ── KI-vs-Code-Reconciliation für Tag-Klassifikation ──
    # Wenn Claude tour-aware Klassifikation geliefert hat: vergleiche mit Code-Werten
    # und nehme die KI-Werte wenn sie deutlich höher sind in Z76 (= Tour-Awareness wirkt).
    # KI ist Source of Truth bei Format-Änderungen die Code-Path nicht versteht.
    final_z72_tage = se_det['z72_tage']
    final_z72_eur  = se_det['z72_eur']
    final_z73_tage = se_det['z73_tage']
    final_z73_eur  = se_det['z73_eur']
    final_z74_tage = se_det['z74_tage']
    final_z74_eur  = se_det['z74_eur']
    final_z76_eur  = se_det['z76_eur']
    if claude_tage_klass:
        ck = claude_tage_klass
        # Plausi: Σ aller Z* sollte ≈ Z77 sein (LH-stfrei-Total)
        ki_sum = ck.get('z72_eur',0) + ck.get('z73_eur',0) + ck.get('z74_eur',0) + ck.get('z76_eur',0)
        code_sum = se_det['z72_eur'] + se_det['z73_eur'] + se_det['z74_eur'] + se_det['z76_eur']
        # KI gewinnt wenn: KI's Z76 deutlich höher (Tour-Awareness wirkt) UND KI-Total plausibel
        z76_diff = ck.get('z76_eur', 0) - se_det['z76_eur']
        if z76_diff > 100 and ki_sum >= z77_total * 0.85:
            final_z72_tage = ck.get('z72_tage', se_det['z72_tage'])
            final_z72_eur  = round(ck.get('z72_eur',  se_det['z72_eur']), 2)
            final_z73_tage = ck.get('z73_tage', se_det['z73_tage'])
            final_z73_eur  = round(ck.get('z73_eur',  se_det['z73_eur']), 2)
            final_z74_tage = ck.get('z74_tage', se_det['z74_tage'])
            final_z74_eur  = round(ck.get('z74_eur',  se_det['z74_eur']), 2)
            final_z76_eur  = round(ck.get('z76_eur',  se_det['z76_eur']), 2)
            print(f"[SE-reconcile] KI gewinnt (Tour-Aware): Code-Z76={se_det['z76_eur']:.2f}€ → KI-Z76={final_z76_eur:.2f}€ (+{z76_diff:.2f}€)")
        else:
            print(f"[SE-reconcile] Code gewinnt (KI keine deutliche Verbesserung: Z76 +{z76_diff:.2f}€)")

    return {
        'abrechnungen':          abrechnungen,
        'flugmonate':            sorted(flugmonate),  # echte Monate mit Flügen (1-12)
        'summe_gesamt':          round(sum(a['gesamt'] for a in abrechnungen), 2),
        'summe_steuerfrei':      z77_total,
        'summe_steuerpflichtig': round(sum(a['steuerpflichtig'] for a in abrechnungen), 2),
        # Final-Werte: Code-deterministisch ODER KI-Override bei Tour-Awareness
        'z72_tage': final_z72_tage, 'z72_eur': final_z72_eur,
        'z73_tage': final_z73_tage, 'z73_eur': final_z73_eur,
        'z74_tage': final_z74_tage, 'z74_eur': final_z74_eur,
        'z76_eur':  final_z76_eur,
        # SE-direkte Counts (überlebt jetzt ohne Flugstundenübersicht)
        'arbeitstage_se':  se_det.get('arbeitstage', 0),
        'fahrtage_se':     se_det.get('fahrtage', 0),
        'hotelnaechte_se': se_det.get('hotelnaechte', 0),
        'unklare_zeilen': se_det['unklare_zeilen'],
    }

def parse_dienstplan_mit_ki(pdf_bytes_list, se_bytes_list=None, km_form=0, se_hints=None, homebase='FRA', einsatzplan_bytes_list=None):
    """
    Analysiert Lufthansa Flugstunden-Übersichten mit Claude (pure KI, kein Regex).
    Claude liest die PDFs direkt und berechnet alle Werte intelligent.
    km kommt vom Nutzer-Formular, wird als Parameter übergeben.
    """
    import anthropic, base64, pdfplumber, io, re, json

    if not pdf_bytes_list:
        return None

    ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    if not ANTHROPIC_KEY:
        return None

    # ── Auswertungs-PDF-Erkennung ──────────────────────────────────────────
    combined = ''
    for pb in _bytes_list(pdf_bytes_list)[:2]:
        try:
            with pdfplumber.open(io.BytesIO(pb)) as pdf:
                combined += ' '.join(p.extract_text() or '' for p in pdf.pages[:3])
        except: pass

    if re.search(r'Steuer-Auswertung|Zeile 72|Zeile 73|Anlage N.*Auswertung', combined, re.I):
        # Auswertungs-PDF: direkt mit Claude parsen
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
            result = {'fahr_tage':0,'km':0,'arbeitstage':0,'hotel_naechte':0,
                      'vma_72_tage':0,'vma_73_tage':0,'vma_74_tage':0,
                      'vma_72':0,'vma_73':0,'vma_74':0,'vma_aus':0,'z77':0,'ausland_touren':[]}
            content_v = []
            for pb in _bytes_list(pdf_bytes_list)[:3]:
                b64 = base64.standard_b64encode(pb).decode()
                content_v.append({'type':'document','source':{'type':'base64','media_type':'application/pdf','data':b64}})
            content_v.append({'type':'text','text':
                'Auswertungs-PDF. Extrahiere: Zeile 72 (Tage, €), 73 (Tage, €), 74 (Tage, €), 76 (€), Fahrtage, km, Arbeitstage, Hotelaufenthalte.\n'
                'JSON: {"vma_72_tage":13,"vma_72":182.0,"vma_73_tage":10,"vma_73":140.0,"vma_74_tage":0,"vma_74":0.0,"vma_aus":4562.0,"fahr_tage":53,"km":27,"arbeitstage":129,"hotel_naechte":54}'
            })
            resp = client.messages.create(model='claude-sonnet-4-6',max_tokens=400,
                messages=[{'role':'user','content':content_v}])
            d = json.loads(re.sub(r'```json|```','',resp.content[0].text.strip()).strip())
            for k,v in d.items():
                result[k] = int(float(v)) if k in ('vma_72_tage','vma_73_tage','vma_74_tage','fahr_tage','km','arbeitstage','hotel_naechte') else float(v)
            print(f"Parser: fahr={result['fahr_tage']} km={result['km']} arbeit={result['arbeitstage']} hotel={result['hotel_naechte']} vma76={result['vma_aus']}")
            return result
        except Exception as e:
            print(f'Parser error: {e}')
            return None

    # ── Reine LH Flugstunden: 100% Claude ──────────────────────────
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)

        # km aus Nutzer-Formular — Fallback 28 wenn nicht angegeben
        km = km_form if km_form and km_form > 0 else 28

        # Flugstunden: alle Seiten aller PDFs als ein Textblock
        content = []
        alle_seiten = []
        pdf_count = len(_bytes_list(pdf_bytes_list)[:12])
        for pb in _bytes_list(pdf_bytes_list)[:12]:
            try:
                with pdfplumber.open(io.BytesIO(pb)) as pdf:
                    for page in pdf.pages:
                        text = page.extract_text() or ''
                        if text.strip():
                            alle_seiten.append(text)
            except:
                pass
        print(f"Flugstunden: {len(alle_seiten)} Seiten extrahiert aus {pdf_count} PDF(s)")

        # ── DETERMINISTISCHES LESEN der Flugstunden ──
        flug_det = None
        if alle_seiten:
            flug_gesamt = '\n\n---\n\n'.join(alle_seiten)
            flug_det = _parse_flugstunden_deterministic(flug_gesamt, homebase=homebase)
            print(f"Flugstunden deterministisch: fahrtage={flug_det['fahrtage']}  arbeitstage={flug_det['arbeitstage']}  hotel={flug_det['hotel_naechte']}  frei={flug_det['frei_tage']}  unklar={len(flug_det['unklare_tage'])} Tage")
            content.append({'type':'text','text':f'FLUGSTUNDEN-ÜBERSICHTEN ({len(alle_seiten)} Seiten, komplettes Steuerjahr):\n\n{flug_gesamt}'})
        else:
            for pb in _bytes_list(pdf_bytes_list)[:5]:
                b64 = base64.standard_b64encode(pb).decode()
                content.append({'type':'document','source':{'type':'base64','media_type':'application/pdf','data':b64}})

        # SE als Klartext (Claude liest Zahlen direkter aus Text als aus PDF-Bild)
        se_kontext = ''
        if se_bytes_list:
            se_texts = []
            for pb in _bytes_list(se_bytes_list)[:12]:
                try:
                    with pdfplumber.open(io.BytesIO(pb)) as pdf:
                        t = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                        if t.strip(): se_texts.append(t)
                except: pass
            if se_texts:
                se_kontext = '\n\nSTRECKENEINSATZ-ABRECHNUNGEN (alle Monate):\n' + '\n---\n'.join(se_texts)

        # CAS-Einsatzplan als zusätzlicher Klartext-Cross-Check (sehr detailliert pro Tag)
        einsatzplan_kontext = ''
        if einsatzplan_bytes_list:
            ep_texts = []
            for pb in _bytes_list(einsatzplan_bytes_list)[:14]:
                try:
                    with pdfplumber.open(io.BytesIO(pb)) as pdf:
                        t = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                        if t.strip(): ep_texts.append(t)
                except: pass
            if ep_texts:
                einsatzplan_kontext = ('\n\nCAS-EINSATZPLAN (PUB-Liste, hoch detailliert pro Tag — '
                                       'mit Briefingzeit, exakter Routing, Tour-Code) — nutze als Cross-Check '
                                       'gegen Flugstunden:\n' + '\n---\n'.join(ep_texts))

        # ── DETERMINISTISCHE BACKEND-AUSWERTUNG (literal aus Doku) ──
        rechner_kontext = ''
        if se_hints or flug_det:
            parts_kontext = ['\n\nDETERMINISTISCHES LESEN VOM BACKEND (Backend hat Zeile für Zeile literal aus den Dokumenten extrahiert — keine Schätzungen):\n']
            if se_hints:
                z77_t  = se_hints.get('summe_steuerfrei', 0)
                z72_t  = se_hints.get('z72_tage', 0); z72_e = se_hints.get('z72_eur', 0)
                z73_t  = se_hints.get('z73_tage', 0); z73_e = se_hints.get('z73_eur', 0)
                z74_t  = se_hints.get('z74_tage', 0); z74_e = se_hints.get('z74_eur', 0)
                z76_e  = se_hints.get('z76_eur', 0)
                se_unklar = len(se_hints.get('unklare_zeilen', []))
                mt     = len(se_hints.get('abrechnungen', []))
                parts_kontext.append(
                    f'\n[SE-Auswertung — was LH stfrei bezahlt hat]\n'
                    f'- Z77 (steuerfrei gesamt): {z77_t:.2f} €\n'
                    f'- Z73 (An-/Abreise mit Übernachtung): {z73_t} Tage / {z73_e:.2f} €\n'
                    f'- Z74 (Inland 24h, selten): {z74_t} Tage / {z74_e:.2f} €\n'
                    f'- Z76 (Ausland-VMA, Σ stfrei-Werte): {z76_e:.2f} €\n'
                    f'- SE-Monate hochgeladen: {mt} von 12\n'
                    f'- Unklare SE-Zeilen: {se_unklar}\n'
                    f'\nZ72 (Inland-Tagestrip >8h) ist NICHT in dieser SE-Auswertung — '
                    f'LH zahlt diese oft nicht stfrei aus. Du musst Z72 selbst aus den '
                    f'Flugstunden zählen: jeden Tag mit A {homebase}→XXX UND E XXX→{homebase} '
                    f'am gleichen Tag, wo XXX ein deutscher Flughafen ist und Gesamtabwesenheit >8h.\n'
                )
            if flug_det:
                parts_kontext.append(
                    f'\n[Flugstunden-Auswertung]\n'
                    f'- Fahrtage: {flug_det["fahrtage"]}\n'
                    f'- Arbeitstage: {flug_det["arbeitstage"]}\n'
                    f'- Hotelnächte: {flug_det["hotel_naechte"]}\n'
                    f'- Frei-Tage: {flug_det["frei_tage"]}\n'
                    f'- Unklare Tage: {len(flug_det["unklare_tage"])}\n'
                )
                if flug_det['unklare_tage']:
                    parts_kontext.append('Unklare Tage (interpretier diese):\n  ' + '\n  '.join(flug_det['unklare_tage'][:30]) + '\n')
            parts_kontext.append(
                '\nWICHTIG: Wenn 0 unklare Zeilen/Tage → übernimm die Backend-Werte 1:1, du musst nichts neu zählen.\n'
                'Wenn unklar > 0 → prüf nur diese spezifischen Zeilen/Tage und sag wie sie zu klassifizieren sind.\n'
                'Mathematischer Plausi-Check: Z72_eur + Z73_eur + Z74_eur + Z76 sollte ≈ Z77 sein.\n'
            )
            rechner_kontext = ''.join(parts_kontext)

        # Referenz-Auswertung als letztes Content-Element (Lernbeispiel, kein Regelwerk)
        ref_kontext = ''
        try:
            fm_ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_faelle.txt')
            if os.path.exists(fm_ref):
                with open(fm_ref, encoding='utf-8') as fmf:
                    ref_kontext = '\n\nHIER SIND ZWEI BEREITS BERECHNETE FÄLLE ZUM VERGLEICH (intern verifiziert intern — nicht als Regeln, sondern als Beispiele zum Lernen):\n' + fmf.read()
        except: pass

        # EASA + Steuerrecht-Referenz als Wissens-Buch
        easa_kontext = ''
        try:
            easa_ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_easa.txt')
            if os.path.exists(easa_ref):
                with open(easa_ref, encoding='utf-8') as ef:
                    easa_kontext = '\n\n═══ FACH-WISSEN: EASA-FTL + DEUTSCHES STEUERRECHT (zum Nachschlagen, nicht als Befehl) ═══\n' + ef.read()
        except: pass

        content.append({'type': 'text', 'text': f"""Du bist ein DOKUMENTEN-LESER für Lufthansa-Kabinenpersonal-Auswertungen.
Deine Aufgabe ist NICHT zu interpretieren oder zu schätzen — sondern Tag für Tag das zu zählen was im Dokument steht.

REGEL #1: Jeder Tag des Jahres hat in den LH-Dokumenten einen Marker. Lies den Marker, klassifiziere danach. KEINE Interpretation.
REGEL #2: Es gibt KEINE "normalen Bandbreiten" für Arbeitstage/Fahrtage/Hotel — die Zahlen kommen aus dem Dokument, nicht aus deinem Wissen.
REGEL #3: Wenn der CAS-Einsatzplan beigefügt ist, nutze ihn als primäre Quelle (detaillierter als Flugstunden) und Cross-Check.

{se_kontext}{einsatzplan_kontext}{rechner_kontext}{ref_kontext}{easa_kontext}

HOMEBASE des Mandanten: **{homebase}**

═══ MARKER-KATALOG (lese, klassifiziere, zähle) ═══

**Frei-Tage (NICHT Arbeitstag, NICHT Fahrtag):**
- `/- FREIER TAG`, `FREI`
- `U` / `URLAUB`
- `K` / `KRANK`
- unbezahlte Freistellung
- `LM NACHGEWAEHRUNG` (Lohnbuchungspost ohne Dienst)

**Tour-Tage (Arbeitstag + Fahrtag bei Tour-Start):**
- `LH#### A {homebase}` = Tour-Start ab Heimatflughafen → Arbeitstag UND Fahrtag (1 Fahrtag pro Tour, nicht pro Tag)
- `LH#### E xxx → {homebase}` = Tour-Ende → Arbeitstag, kein neuer Fahrtag
- `FL STRECKENEINSATZTAG` = Layover-Tag im Ausland → Arbeitstag UND Hotel-Nacht
- Mehretappen ohne {homebase}-Touch (z.B. FRA→GVA→OTP→FRA) = 1 Fahrtag total

**Home-Duty (Arbeitstag, KEIN Fahrtag — User war zuhause):**
- `SBY` (Standby zuhause)
- `RES` (Reserve zuhause)
- Online-Schulung, e-Learning
- Webinar von zuhause

**Office/Vor-Ort-Dienst in {homebase} (Arbeitstag UND Fahrtag — User MUSSTE zum Flughafen):**
- `EK BÜRODIENST`, `EK` (Bürodienst)
- `EM` (Erste-Hilfe-Maßnahmen / Briefing)
- `D4` Schulung in Präsenz (typisch mehrtägig — JEDER Tag = Arbeitstag + Fahrtag)
- `DD SEMINAR`, `DD ABORDNUNG` — JEDER Tag des Seminars = Arbeitstag + Fahrtag (täglich An-/Abreise)
- `EH` (Erste Hilfe), Sprachtest, Medical Check-up
- `BRIEFING` mit Uhrzeit

**EASA-FTL Layover-Regel (für Hotel-Nacht-Erkennung):**
FL-Marker bei LH = echter Layover mit ≥10h Bodenzeit = 1 Hotelnacht.
Bei mehrtägiger Auslandstour: jeder FL-Tag UND jeder Tag zwischen A und E im Ausland = Hotelnacht.

**Briefingzeiten LH-Kabine (für Abwesenheits-Berechnung Z72-Tagestrip 8h-Schwelle):**
- Wenn die Briefingzeit explizit im Dienstplan/Einsatzplan steht → diese benutzen (z.B. "Briefingzeit(LT FRA): 03/02/25 20:10")
- Wenn nicht ablesbar, dann Faustregel:
  → Kurzstrecke (Block ≤ 4h):  **85 Min** Briefing vor STD (= 1:25 h)
  → Langstrecke (Block > 4h):  **110 Min** Briefing vor STD (= 1:50 h)
- Plus 30 Min Sign-Off (Nacharbeitung) nach Block-In, einheitlich für alle Tour-Typen
- Plus Anfahrt (User-spezifisch, ggf. aus km abgeleitet: km × 1,5 min)
- Tagestrip qualifiziert für Z72 wenn: anfahrt + briefing + block + 30 + anfahrt ≥ 480 min (8h)

═══ ZÄHL-METHODE — sei DUMM und gründlich ═══

Schritt 1: Gehe Tag für Tag durch (1.1. bis 31.12.).
Schritt 2: Pro Tag: lies den Marker.
Schritt 3: Klassifiziere nach obigem Katalog.
Schritt 4: Zähle in der richtigen Kategorie.

Beispiel Januar mit User-Daten:
- 01.-07.01: FREIE TAGE → 0 Arbeitstage, 0 Fahrtage, 0 Hotel
- 08.01: A FRA→NQZ → Arbeitstag + Fahrtag (Tour-Start)
- 09.-13.01: FL → 5 Arbeitstage + 5 Hotelnächte
- 14.01: E NQZ→FRA → Arbeitstag (Tour-Ende, kein Fahrtag)
- 15.01: weiter Folge der Tour → Arbeitstag, kein Hotel
- ... usw.

═══ VMA-WERTE (aus SE-Klartext direkt addieren) ═══

vma_aus = Σ aller stfrei-Werte wo stfrei-Ort AUSLAND ist
vma_72/73/74 = Σ stfrei-Werte wo stfrei-Ort INLAND, klassifiziert per Tag
z77 = lass auf 0 (Backend rechnet)

WICHTIG: Klassifiziere konservativ wie ein klassischer Steuerberater (branchenüblicher Steuerberater-Methode):
- Wenn LH am Anreise-Tag mit FRA stfrei-Ort 14€ schreibt → Z73 (Inland-An-/Abreise) bleibt 14€
- Wenn LH am gleichen Tag eine ZUSÄTZLICHE Zeile mit Auslands-Stempel schreibt → das ist Z76
- Z76 = NUR was LH explizit mit Ausland-Stempel auszeichnet
- Inland-Anteile von Auslandstouren NICHT künstlich aufwerten

═══ LIEFERFORMAT ═══

Liefere via Tool-Use die Werte. fahrtage/arbeitstage/hotel_naechte sind PFLICHT.
km bleibt {km} (User-Angabe).

Gib im 'nachweis'-Feld eine kurze Monats-Zusammenfassung — pro Monat 1-2 Sätze:
"JAN: 5 Arbeitstage (Tour 08-15.01), 1 Fahrtag, 5 Hotel. URL 1-7.1, FREI 16-31.1."

KEINE Plausi-Bandbreiten in deinem Output — die Zahlen sind die Zahlen. Bei DD SEMINAR mit 15 Tagen ergibt das 15 Arbeitstage + 15 Fahrtage, auch wenn das im Jahr "viel" wirkt.
"""
        })

        import time as _time_mod
        sonnet_start_time = _time_mod.time()
        # ── TOOL-USE statt Prompt-JSON ──
        # Tool-Use ist offizielle Anthropic-Feature für strukturierte Outputs.
        # Zwingt Sonnet zu sofortigem JSON ohne 20k-Zeichen-Reasoning-Vorgeplänkel.
        # Erwartete Zeit: 30-60s statt 180s.
        dp_tool = {
            'name': 'submit_dienstplan_analysis',
            'description': 'Submit the analyzed Lufthansa Dienstplan values',
            'input_schema': {
                'type': 'object',
                'properties': {
                    'fahrtage':       {'type': 'integer', 'description': 'Anzahl Fahrtage zum Flughafen (eine Tour = 1 Fahrtag)'},
                    'arbeitstage':    {'type': 'integer', 'description': 'Alle Dienst-Tage (Flug, Standby, Schulung, Office) ohne Frei/Urlaub/Krank'},
                    'hotel_naechte':  {'type': 'integer', 'description': 'Auslands-Übernachtungen mit ≥10h Bodenzeit (EASA-FTL)'},
                    'vma_72_tage':    {'type': 'integer', 'description': 'Inland-Tagestrips >8h (Z72)'},
                    'vma_72':         {'type': 'number',  'description': 'Z72 EUR-Summe'},
                    'vma_73_tage':    {'type': 'integer', 'description': 'An-/Abreisetage Inland (Z73)'},
                    'vma_73':         {'type': 'number',  'description': 'Z73 EUR-Summe'},
                    'vma_74_tage':    {'type': 'integer', 'description': 'Inland 24h-Tage (Z74, selten)'},
                    'vma_74':         {'type': 'number',  'description': 'Z74 EUR-Summe'},
                    'vma_aus':        {'type': 'number',  'description': 'VMA Ausland Z76 (Σ stfrei-Werte aus SE für Auslandsdestinationen)'},
                    'z77':            {'type': 'number',  'description': 'Z77 stfrei gesamt — auf 0 lassen, Backend rechnet'},
                    'km':             {'type': 'integer', 'description': 'km Homebase einfache Strecke'},
                    'nachweis':       {'type': 'string',  'description': 'Knappe Begründung Monat für Monat (1-2 Sätze pro Monat)'},
                },
                'required': ['fahrtage', 'arbeitstage', 'hotel_naechte'],
            }
        }
        # Tool-Use Call mit Retry-Logik (transient errors)
        tool_resp = None
        last_err = None
        for attempt in range(3):
            try:
                tool_resp = client.messages.create(
                    model='claude-sonnet-4-6',
                    max_tokens=8000,
                    tools=[dp_tool],
                    tool_choice={'type': 'tool', 'name': 'submit_dienstplan_analysis'},
                    messages=[{'role': 'user', 'content': content}],
                )
                break
            except Exception as e:
                last_err = e
                err_str = str(e)
                should_retry = any(s in err_str.lower() for s in ['429','500','502','503','504','timeout','connection','rate'])
                if not should_retry or attempt == 2:
                    raise
                wait = 2 ** attempt + 1
                print(f"[Sonnet-DP-tool] retry {attempt+1}/3 in {wait}s — {err_str[:120]}")
                _time_mod.sleep(wait)
        if tool_resp is None:
            raise (last_err or RuntimeError('Sonnet-DP tool call fehlgeschlagen'))
        elapsed = _time_mod.time() - sonnet_start_time
        # max_tokens-Truncation erkennen — kann unvollständige tool_input liefern
        if getattr(tool_resp, 'stop_reason', None) == 'max_tokens':
            print(f"[Sonnet-DP] WARNUNG: stop_reason='max_tokens' nach {elapsed:.1f}s — tool_input ggf. unvollständig")
        # Tool-Use-Response: content ist Liste, einer der Blöcke ist tool_use mit input-Dict
        tool_input = None
        nachweis_text = ''
        for block in tool_resp.content:
            if getattr(block, 'type', None) == 'tool_use':
                tool_input = block.input
            elif getattr(block, 'type', None) == 'text':
                nachweis_text = (nachweis_text + ' ' + (block.text or '')).strip()
        if not tool_input:
            raise RuntimeError('Sonnet-DP tool_use lieferte keine input')
        print(f"Sonnet-DP tool_use OK: {elapsed:.1f}s, fields={list(tool_input.keys())}")
        # full_text für nachgelagerte JSON-Extraktion (brace-counter) — nachweis nur einmal
        import json as _json
        tool_input_for_text = {k: v for k, v in tool_input.items() if k != 'nachweis'}
        full_text = _json.dumps(tool_input_for_text, ensure_ascii=False) + '\n\n' + (tool_input.get('nachweis') or nachweis_text or '')

        # ── JSON robust extrahieren via brace-counter (String-aware) ──
        nachweis = ''
        json_str = '{}'
        candidates = []
        depth = 0
        start = -1
        in_str = False; escape_next = False
        for i, ch in enumerate(full_text):
            if escape_next:
                escape_next = False
                continue
            if in_str:
                if ch == '\\': escape_next = True
                elif ch == '"': in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == '{':
                if depth == 0: start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append((start, i+1, full_text[start:i+1]))
                    start = -1
        # Kandidat mit "fahrtage" hat Vorrang
        for cs, ce, cstr in candidates:
            if '"fahrtage"' in cstr:
                json_str = cstr
                nachweis = (full_text[:cs] + full_text[ce:]).strip()
                break
        else:
            # kein "fahrtage"-JSON — nimm den größten Kandidaten als Best-Effort
            if candidates:
                cs, ce, cstr = max(candidates, key=lambda x: len(x[2]))
                json_str = cstr
                nachweis = (full_text[:cs] + full_text[ce:]).strip()

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as je:
            print(f"Claude JSON parse failed: {je}")
            print(f"Full response (first 1500 chars):\n{full_text[:1500]}")
            parsed = {}

        if not parsed.get('arbeitstage'):
            print(f"⚠️ Claude lieferte kein arbeitstage. Volle Antwort (erste 2000 Zeichen):\n{full_text[:2000]}")
        print(f"Claude: fahr={parsed.get('fahrtage')} arbeit={parsed.get('arbeitstage')} hotel={parsed.get('hotel_naechte')} z77={parsed.get('z77')}")
        if nachweis:
            print(f"Nachweis:\n{nachweis[:800]}")

        # ── HYBRID: Deterministisch + Claude + Opus-Verifikation ──
        sonnet_elapsed = _time_mod.time() - sonnet_start_time
        flug_clean = flug_det and len(flug_det.get('unklare_tage', [])) == 0
        opus_result = None
        verification_source = 'parser-only'

        if flug_clean:
            fahrtage_final   = flug_det['fahrtage']
            arbeitstage_fin  = flug_det['arbeitstage']
            hotel_final      = flug_det['hotel_naechte']
            verification_source = 'parser-deterministisch'
            print(f"Flugstunden-Werte: aus deterministischem Parser (0 unklare Tage) übernommen")
        else:
            p_f = flug_det['fahrtage'] if flug_det else 0
            p_a = flug_det['arbeitstage'] if flug_det else 0
            p_h = flug_det['hotel_naechte'] if flug_det else 0
            c_f = int(parsed.get('fahrtage') or 0)
            c_a = int(parsed.get('arbeitstage') or 0)
            c_h = int(parsed.get('hotel_naechte') or 0)

            # Opus läuft als Senior-Verifikation, AUSSER:
            # - Sonnet hat schon >180s gebraucht (Render-Timeout-Risiko)
            # - Sonnet+Parser sind sehr nah dran (<2 Tage Diff aller Werte) — keine Verifikation nötig
            f_diff = abs(p_f - c_f)
            a_diff = abs(p_a - c_a)
            h_diff = abs(p_h - c_h)
            small_diff = f_diff <= 2 and a_diff <= 3 and h_diff <= 2
            run_opus = not small_diff and sonnet_elapsed < 180

            if not run_opus:
                print(f"Opus übersprungen — Konsensus zwischen Parser und Sonnet (diff={f_diff}/{a_diff}/{h_diff}) oder Sonnet-Zeit ({sonnet_elapsed:.0f}s) zu lang")

            if run_opus:
                # Opus 4.7 läuft immer für maximale Genauigkeit
                print(f"Opus-Verifikation startet: parser=({p_f}/{p_a}/{p_h})  sonnet=({c_f}/{c_a}/{c_h})  unklar_flug={len(flug_det['unklare_tage']) if flug_det else '-'}  unklar_se={len(se_hints.get('unklare_zeilen', [])) if se_hints else '-'}")
                parser_sum = (
                    f"Parser-Werte: fahrtage={p_f}, arbeitstage={p_a}, hotel={p_h}\n"
                    f"Unklare Flugstunden-Tage ({len(flug_det['unklare_tage']) if flug_det else 0}): "
                    + ('; '.join(flug_det['unklare_tage'][:15]) if flug_det else 'keine')
                )
                if se_hints:
                    parser_sum += f"\nSE-Werte: Z72={se_hints.get('z72_tage',0)}T/{se_hints.get('z72_eur',0)}€, Z73={se_hints.get('z73_tage',0)}T/{se_hints.get('z73_eur',0)}€, Z76={se_hints.get('z76_eur',0)}€, unklar={len(se_hints.get('unklare_zeilen', []))} Zeilen"
                sonnet_sum = (
                    f"Sonnet-Werte: fahrtage={c_f}, arbeitstage={c_a}, hotel={c_h}, "
                    f"vma_72={parsed.get('vma_72_tage')}T/{parsed.get('vma_72')}€, vma_73={parsed.get('vma_73_tage')}T/{parsed.get('vma_73')}€, vma_aus={parsed.get('vma_aus')}€"
                )
                full_se_text = ''
                if se_bytes_list:
                    for pb in _bytes_list(se_bytes_list)[:12]:
                        try:
                            with pdfplumber.open(io.BytesIO(pb)) as pdf:
                                full_se_text += '\n'.join(p.extract_text() or '' for p in pdf.pages) + '\n'
                        except: pass
                opus_result = _opus_verifizierung(parser_sum, sonnet_sum, full_se_text, flug_gesamt if alle_seiten else '')

            if opus_result:
                # Defensive Merging: Opus-Wert nur nehmen wenn er nicht 0 ist UND Claude/Parser haben höhere Werte
                _o_f = int(opus_result.get('fahrtage') or 0)
                _o_a = int(opus_result.get('arbeitstage') or 0)
                _o_h = int(opus_result.get('hotel_naechte') or 0)
                fahrtage_final  = _o_f if _o_f > 0 else max(p_f, c_f)
                arbeitstage_fin = _o_a if _o_a > 0 else max(p_a, c_a)
                hotel_final     = _o_h if _o_h > 0 else max(p_h, c_h)
                # Opus überschreibt VMA-Werte ggf. auch
                if opus_result.get('vma_aus'):
                    parsed['vma_aus'] = float(opus_result['vma_aus'])
                if opus_result.get('vma_72_tage') is not None:
                    parsed['vma_72_tage'] = int(opus_result['vma_72_tage'])
                    parsed['vma_72'] = float(opus_result.get('vma_72', 0))
                if opus_result.get('vma_73_tage') is not None:
                    parsed['vma_73_tage'] = int(opus_result['vma_73_tage'])
                    parsed['vma_73'] = float(opus_result.get('vma_73', 0))
                if opus_result.get('vma_74_tage') is not None:
                    parsed['vma_74_tage'] = int(opus_result['vma_74_tage'])
                    parsed['vma_74'] = float(opus_result.get('vma_74', 0))
                verification_source = 'opus-verifiziert'
            else:
                # Kein Opus oder kein Konflikt — Hybrid: bevorzuge Parser außer wenn Claude höher (Parser kann Tage übersehen)
                fahrtage_final  = max(p_f, c_f)
                arbeitstage_fin = max(p_a, c_a)
                hotel_final     = max(p_h, c_h)
                verification_source = 'sonnet-hybrid'
            print(f"Flugstunden final via {verification_source}: fahrtage={fahrtage_final}, arbeitstage={arbeitstage_fin}, hotel={hotel_final}")

        return {
            'fahr_tage':    fahrtage_final,
            'km':           int(parsed.get('km', km)),
            'arbeitstage':  arbeitstage_fin,
            'hotel_naechte':hotel_final,
            # VMA-Werte: Claude ist primary; Parser-Z72 als Fallback wenn Claude 0 hat
            'vma_72_tage':  int(parsed.get('vma_72_tage') or (flug_det.get('z72_inland_days', 0) if flug_det else 0)),
            'vma_73_tage':  int(parsed.get('vma_73_tage', 0)),
            'vma_74_tage':  int(parsed.get('vma_74_tage', 0)),
            'vma_72':       float(parsed.get('vma_72') or ((flug_det.get('z72_inland_days', 0) if flug_det else 0) * 14.0)),
            'vma_73':       float(parsed.get('vma_73', 0)),
            'vma_74':       float(parsed.get('vma_74', 0)),
            'vma_aus':      float(parsed.get('vma_aus', 0)),
            'z77':          float(parsed.get('z77', 0)),
            'nachweis':     nachweis,
            'ausland_touren': [],
            # Audit-Trail für PDF
            '_flug_parser':   flug_det,
            '_flug_claude':   {'fahrtage': parsed.get('fahrtage'), 'arbeitstage': parsed.get('arbeitstage'), 'hotel_naechte': parsed.get('hotel_naechte')},
            '_flug_clean':    flug_clean,
            '_opus_used':     opus_result is not None,
            '_opus_nachweis': (opus_result or {}).get('_opus_nachweis', ''),
            '_verification_source': verification_source,
        }

    except Exception as e:
        print(f'Claude Flugstunden error: {e}')
        raise RuntimeError(f'Steuerberechnung fehlgeschlagen: {e}')


def _opus_verifizierung(parser_summary, sonnet_summary, full_se_text, full_flug_text):
    """Opus 4.7 als Senior-Werbungskosten-Klassifikator (branchenübliche Steuer-Praxis). Wird nur gerufen wenn Parser+Sonnet uneinig sind.
    Bekommt beide Vorschläge + Originaldokumente, entscheidet final.
    Liefert verifizierte Werte + Begründung.
    """
    ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    if not ANTHROPIC_KEY:
        return None
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        prompt = f"""Du bist ein erfahrener Werbungskosten-Klassifikator für Lufthansa-Kabinenpersonal mit jahrzehntelanger Erfahrung in branchenüblicher Steuer-Praxis.
Zwei Junior-Berater haben unabhängig dieselben Dokumente ausgewertet und kommen zu unterschiedlichen Werten.
Deine Aufgabe: Streit schlichten — den korrekten Wert ermitteln, nicht den Mittelwert.

JUNIOR 1 (Deterministischer Parser, liest literal aus Dokument):
{parser_summary}

JUNIOR 2 (KI-Werbungskosten-Klassifikator Sonnet, interpretiert Edge-Cases):
{sonnet_summary}

ORIGINAL-DOKUMENTE:

[STRECKENEINSATZ]
{full_se_text[:30000]}

[FLUGSTUNDEN]
{full_flug_text[:30000]}

Schlichte den Konflikt indem du die Originaldokumente selbst liest. §9 EStG + EU 965/2012 EASA-FTL gelten.
Antwort ZUERST als JSON (erste Zeile), dann Begründung:

{{"fahrtage":N,"arbeitstage":N,"hotel_naechte":N,"vma_72_tage":N,"vma_72":F,"vma_73_tage":N,"vma_73":F,"vma_74_tage":N,"vma_74":F,"vma_aus":F}}

Kurze Begründung wo Junior 1 vs 2 falsch lagen.
"""
        resp = _claude_with_retry(client, 'claude-opus-4-7', 8000, prompt,
                                   max_retries=3, label='Opus-Verify')
        full_text = resp.content[0].text.strip()

        # Brace-counter JSON-Extraktion (gleiche Logik wie bei Sonnet)
        json_str = '{}'
        nachweis = full_text
        candidates = []
        depth = 0; start = -1
        in_str = False; escape_next = False
        for i, ch in enumerate(full_text):
            if escape_next:
                escape_next = False
                continue
            if in_str:
                if ch == '\\': escape_next = True
                elif ch == '"': in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == '{':
                if depth == 0: start = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start >= 0:
                    candidates.append((start, i+1, full_text[start:i+1]))
                    start = -1
        for cs, ce, cstr in candidates:
            if '"fahrtage"' in cstr:
                json_str = cstr
                nachweis = (full_text[:cs] + full_text[ce:]).strip()
                break

        try:
            verifiziert = json.loads(json_str)
        except Exception as je:
            print(f"Opus JSON parse failed: {je}; full text: {full_text[:1000]}")
            return None
        print(f"Opus-Verifikation: fahr={verifiziert.get('fahrtage')} arbeit={verifiziert.get('arbeitstage')} hotel={verifiziert.get('hotel_naechte')} z76={verifiziert.get('vma_aus')}")
        if nachweis:
            print(f"Opus-Begründung:\n{nachweis[:600]}")
        return {**verifiziert, '_opus_nachweis': nachweis[:800]}
    except Exception as e:
        print(f"Opus-Verifikation fehlgeschlagen: {e}")
        return None


def _einsatzplan_extract_via_regex(full_text):
    """Regex-Extraktion eines Monats. Liefert dict mit monat_str, umlaeufe."""
    import re as _re
    m_monat = _re.search(
        r'\b(JAN|FEB|M[ÄA]R|APR|MAI|JUN|JUL|AUG|SEP|OKT|NOV|DEZ)\s+(\d{4})',
        full_text[:600])
    monat_str = f"{m_monat.group(1)} {m_monat.group(2)}" if m_monat else "?"

    umlaeufe = []
    umlauf_pattern = _re.compile(
        r'^(?:Mo|Di|Mi|Do|Fr|Sa|So)\s+(\d{1,2})\s+(\d{4,6})\s+FB[^\n]*?(\d+(?:[.,]\d+)?)\s+EURO',
        _re.M)
    tg_pattern = _re.compile(r'(\d+)\s*Tg\s+\d+(?:[.,]\d+)?\s+EURO')

    for match in umlauf_pattern.finditer(full_text):
        tag_nr, umlauf_nr, spesen_str = match.groups()
        line = match.group(0)
        try:
            spesen = float(spesen_str.replace(',', '.'))
        except Exception:
            continue
        tg_match = tg_pattern.search(line)
        tage = int(tg_match.group(1)) if tg_match else 0
        umlaeufe.append({
            'umlauf_nr': umlauf_nr,
            'monat': monat_str,
            'tage': tage,
            'spesen_eur': spesen,
            'ist_tagestrip': (tage == 1),
        })
    return {'monat_str': monat_str, 'umlaeufe': umlaeufe}


def _einsatzplan_extract_via_claude(full_text, regex_hint=None, label='einsatzplan'):
    """Claude liest den Einsatzplan-Text strukturiert. Source of Truth."""
    if not ANTHROPIC_KEY or not full_text:
        return {}
    hint_block = ''
    if regex_hint and regex_hint.get('umlaeufe'):
        hint_block = (
            f"\nRegex-Vorschlag (kann falsch sein, prüfe selbst):\n"
            f"  Monat: {regex_hint.get('monat_str','?')}\n"
            f"  Umläufe: {len(regex_hint['umlaeufe'])} (Spesen-Total {sum(u['spesen_eur'] for u in regex_hint['umlaeufe']):.2f}€)\n")
    prompt = (
        "Du liest einen CAS-Einsatzplan (PUB-Liste) der Lufthansa für einen Monat. "
        "Extrahiere alle Umläufe (=Touren) als JSON-Array. Gib NUR JSON zurück, kein Erklärtext.\n\n"
        "Format:\n"
        '{\n'
        '  "monat": "MAR 2025",\n'
        '  "umlaeufe": [\n'
        '    {"umlauf_nr": "60039", "tag_nr": 3, "tage": 4, "spesen_eur": 206.00},\n'
        '    {"umlauf_nr": "60379", "tag_nr": 12, "tage": 1, "spesen_eur": 148.00}\n'
        '  ]\n'
        '}\n\n'
        "Regeln:\n"
        "- Pro Tour = 1 Eintrag, auch wenn Tour mehrere Tage geht\n"
        "- 'tage' = Anzahl Tage der Tour (Tagestrip = 1, Mehrtagestour = 2-5)\n"
        "- 'spesen_eur' = der Gesamt-Spesen-Betrag der Tour in EUR (Wert vor 'EURO')\n"
        "- 'tag_nr' = Tag im Monat (1-31) an dem Tour startet\n"
        "- Storno-Touren NICHT mitzählen\n"
        "- Office-Days, Standby, Frei-Tage NICHT als Umlauf zählen\n"
        + hint_block +
        "\nPDF-Text:\n" + full_text[:15000]
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        resp = _claude_with_retry(client, 'claude-sonnet-4-5', 4000,
                                  [{'type': 'text', 'text': prompt}], label=label)
        ai_text = resp.content[0].text.strip()
        m = re.search(r'\{[\s\S]*\}', ai_text)
        if not m:
            print(f"[Einsatzplan/{label}] kein JSON: {ai_text[:200]}")
            return {}
        import json as _json
        data = _json.loads(m.group(0))
        return {
            'monat_str': data.get('monat', '?'),
            'umlaeufe': [{
                'umlauf_nr': str(u.get('umlauf_nr', '')),
                'monat': data.get('monat', '?'),
                'tage': int(u.get('tage', 0) or 0),
                'spesen_eur': float(u.get('spesen_eur', 0) or 0),
                'ist_tagestrip': int(u.get('tage', 0) or 0) == 1,
            } for u in data.get('umlaeufe', [])],
        }
    except Exception as e:
        print(f"[Einsatzplan/{label}] Claude fail: {e}")
        return {}


def parse_einsatzplan_mit_ki(pdf_bytes_list, year=2025):
    """
    Parsed den CAS-Einsatzplan (PUB-Liste) — pro Monat eine PDF.
    Strategie: Regex + Claude parallel pro Monat. Claude ist Source of Truth.
    Bei Diskrepanz nimmt Claude. Fallback Regex bei API-Ausfall.
    """
    if not pdf_bytes_list:
        return None

    monate_geparst = 0
    umlaeufe = []
    tagestrips = []
    spesen_total = 0.0
    monatslisten = []

    for item in pdf_bytes_list:
        pdf_bytes = item[0] if isinstance(item, tuple) else item
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                full_text = '\n'.join((p.extract_text() or '') for p in pdf.pages)
        except Exception as e:
            print(f"[Einsatzplan] PDF-Read fail: {e}")
            continue

        # Format-Check: stabile CAS-Header bevorzugt — sonst muss zusätzlich
        # ein einsatzplan-spezifischer Marker da sein (verhindert dass SE/DP-PDFs
        # fälschlich als Einsatzplan geparst werden, nur weil "MAR 2025" im Text steht)
        has_cas_header = (
            'Persönlicher Einsatzplan' in full_text or
            'Crew Assignment System' in full_text or
            'PUB-Liste' in full_text
        )
        # Wenn CAS-Header fehlt: zusätzlich nach Einsatzplan-typischen Markern suchen
        # (Briefingzeit, "Tg" + EURO, Monat im Header) UND NICHT nach SE/DP-Markern
        # 'Erstellt' ohne Streckeneinsatz-Kontext könnte auch im Einsatzplan vorkommen — daher enger
        is_se = ('Streckeneinsatz' in full_text or
                 ('Erstellt' in full_text[:500] and ('stfrei' in full_text or 'Summe:' in full_text)))
        is_dp = 'Flugstunden' in full_text or 'Steuer-Auswertung' in full_text
        has_einsatzplan_marker = (
            re.search(r'\bBriefingzeit\b', full_text) or
            re.search(r'\d+\s*Tg\s+\d+', full_text)
        )
        is_einsatzplan = has_cas_header or (
            has_einsatzplan_marker and not is_se and not is_dp and
            re.search(r'\b(JAN|FEB|MAR|MÄR|APR|MAI|JUN|JUL|AUG|SEP|OKT|NOV|DEZ)\s+\d{4}', full_text[:1000])
        )
        if not is_einsatzplan:
            print(f"[Einsatzplan] Format nicht erkannt (CAS-Header fehlt und keine Einsatzplan-Marker; SE={is_se}, DP={is_dp})")
            continue

        # Regex (schnell, deterministisch)
        regex_data = _einsatzplan_extract_via_regex(full_text)
        # Claude (Source of Truth)
        ai_data = _einsatzplan_extract_via_claude(full_text, regex_hint=regex_data,
                                                   label=f'einsatzplan-{monate_geparst+1}')

        # Wer gewinnt? Claude wenn er Werte hat, sonst Regex
        if ai_data.get('umlaeufe'):
            chosen_data = ai_data
            source = 'claude'
        else:
            chosen_data = regex_data
            source = 'regex'

        monat_str = chosen_data['monat_str']
        ai_count = len(ai_data.get('umlaeufe', []))
        rg_count = len(regex_data.get('umlaeufe', []))
        umlauf_count = len(chosen_data.get('umlaeufe', []))

        # Nur als geparst zählen wenn wir tatsächlich Daten haben (Umläufe ODER eindeutiger Monat)
        if umlauf_count == 0 and monat_str == '?':
            print(f"[Einsatzplan] PDF erkannt aber 0 Umläufe + kein Monat → übersprungen")
            continue
        monate_geparst += 1

        if ai_count != rg_count:
            print(f"[Einsatzplan/{monat_str}] Umlauf-Diskrepanz: regex={rg_count} claude={ai_count} → {source}")
        else:
            print(f"[Einsatzplan/{monat_str}] {ai_count} Umläufe, regex+claude einig")

        for u in chosen_data['umlaeufe']:
            umlaeufe.append(u)
            spesen_total += u['spesen_eur']
            if u['ist_tagestrip']:
                tagestrips.append(u)
        monatslisten.append(monat_str)

    if monate_geparst == 0:
        return None

    return {
        'monate_geparst':  monate_geparst,
        'monatslisten':    monatslisten,
        'umlaeufe':        umlaeufe,
        'spesen_total':    round(spesen_total, 2),
        'tagestrips_count': len(tagestrips),
        'tagestrips':      tagestrips,
    }


def _opus_final_audit(values, texts, year):
    """
    Opus 4.7 Senior-Werbungskosten-Review aller berechneten Werte (branchenübliche Steuer-Praxis).
    Bekommt: alle Schlüsselwerte + die Original-PDF-Texte.
    Liefert: Liste von Korrektur-Vorschlägen oder leere Liste wenn alles plausibel.

    Format der Korrekturen: [{'feld': 'z77', 'aktuell': 1234.56, 'korrekt': 1300.00,
                              'grund': '...', 'severity': 'critical|minor'}]
    """
    if not ANTHROPIC_KEY:
        return []
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        # Werte-Zusammenfassung
        v = values
        summary = (
            f"=== AKTUELLE BERECHNUNG (Steuerjahr {year}) ===\n"
            f"Lohnsteuerbescheinigung:\n"
            f"  Brutto:               {v.get('brutto', 0):.2f} €\n"
            f"  Lohnsteuer:           {v.get('lohnsteuer', 0):.2f} €\n"
            f"  Z17 AG-Fahrt:         {v.get('ag_fahrt_z17', 0):.2f} €\n"
            f"  Z20 Verpflegung:      {v.get('verpflegungszuschuss_z20', 0):.2f} €\n"
            f"\nStreckeneinsatz:\n"
            f"  Z77 stfrei gesamt:    {v.get('z77', 0):.2f} €\n"
            f"  Z76 VMA Ausland:      {v.get('vma_aus', 0):.2f} €\n"
            f"  Z72 Tagestrips:       {v.get('vma_72_tage', 0)} Tage / {v.get('vma_72', 0):.2f} €\n"
            f"  Z73 An-/Abreise:      {v.get('vma_73_tage', 0)} Tage / {v.get('vma_73', 0):.2f} €\n"
            f"  Z74 24h Inland:       {v.get('vma_74_tage', 0)} Tage / {v.get('vma_74', 0):.2f} €\n"
            f"\nFlugstunden:\n"
            f"  Arbeitstage:          {v.get('arbeitstage', 0)}\n"
            f"  Fahrtage:             {v.get('fahr_tage', 0)}\n"
            f"  Hotelnächte:          {v.get('hotel_naechte', 0)}\n"
            f"  Fahrtkosten:          {v.get('fahr', 0):.2f} €\n"
            f"\nResultat:\n"
            f"  Reinigung+Trinkgeld:  {v.get('reinig', 0):.2f} + {v.get('trink', 0):.2f} €\n"
            f"  Werbungskosten gesamt:{v.get('gesamt', 0):.2f} €\n"
            f"  ./. Z17 AG-Fahrt:     {v.get('ag_fahrt_z17', 0):.2f} €\n"
            f"  ./. Z77 stfrei:       {v.get('z77', 0):.2f} €\n"
            f"  = Netto-Werbungskosten:{v.get('netto', 0):.2f} €\n"
        )
        # Original-Texte (gekürzt)
        lsb_text = (texts.get('lsb_text') or '')[:5000]
        se_text  = (texts.get('se_text')  or '')[:15000]
        dp_text  = (texts.get('dp_text')  or '')[:8000]

        prompt = (
            "Du bist erfahrener Werbungskosten-Klassifikator für Lufthansa-Kabinenpersonal (branchenübliche Steuer-Praxis). "
            "Ein Junior-Berater hat die Auswertung gemacht. Deine Aufgabe: Plausi-Check über ALLE Werte zusammen — "
            "Math-Check + interne Konsistenz + Cross-Document-Validation.\n\n"
            "Prüfe insbesondere:\n"
            "1. Math: Brutto - Z17 - Z77 - andere Abzüge → ergibt das den Netto-Wert?\n"
            "2. Konsistenz Z77 (LSB nicht direkt gelistet, kommt aus SE) — passt zur Summe der SE-stfrei-Werte?\n"
            "3. Konsistenz Z20 (Verpflegung LSB) ≈ Z77 (stfrei aus SE)? Beide repräsentieren steuerfreie Verpflegung.\n"
            "4. Realistisch: Vollzeit-Kabine ~120-150 Arbeitstage, ~50-65 Fahrtage, ~40-65 Hotelnächte. "
            "   Werte deutlich außerhalb → entweder Teilzeit (OK) oder Fehler.\n"
            "5. VMA-Anteile: Z72+Z73+Z74 sollten ≈ Z77 ergeben (alle stfrei Inland).\n"
            "6. Hotelnächte ≤ Arbeitstage (logisch).\n"
            "7. Fahrtage ≤ Arbeitstage.\n\n"
            f"{summary}\n\n"
            "=== ORIGINAL-DOKUMENTE (gekürzt) ===\n"
            f"[LSB]\n{lsb_text}\n\n[SE]\n{se_text}\n\n[FLUGSTUNDEN]\n{dp_text}\n\n"
            "Antworte als JSON-Array. Wenn alles plausibel: []. Wenn Issues:\n"
            '[\n'
            '  {"feld": "z77", "aktuell": 1234.56, "korrekt": 1300.00, "grund": "...", "severity": "critical"},\n'
            '  {"feld": "arbeitstage", "aktuell": 200, "korrekt": null, "grund": "Sehr hoch — Teilzeit oder zwei Jobs?", "severity": "minor"}\n'
            ']\n\n'
            "Regeln:\n"
            "- 'feld': z77, z76, vma_72, vma_73, vma_74, brutto, lohnsteuer, ag_fahrt_z17, "
            "         arbeitstage, fahr_tage, hotel_naechte, gesamt, netto\n"
            "- 'korrekt': der NEUE Wert wenn du sicher bist; null wenn du nur warnen willst\n"
            "- 'severity': 'critical' = klar falsch, 'minor' = ungewöhnlich aber möglich\n"
            "- KEINE Erklärungstexte außerhalb des JSON-Arrays.\n"
        )
        resp = _claude_with_retry(client, 'claude-opus-4-7', 4000,
                                  [{'type': 'text', 'text': prompt}],
                                  max_retries=2, label='opus-final-audit')
        ai_text = resp.content[0].text.strip()
        # Robuster JSON-Array-Extraktor: balanced bracket counter MIT String-Awareness
        # (sonst würde ']' in einem JSON-String wie "grund": "Wert]: zu hoch" den Counter falsch dekrementieren)
        import json as _json
        issues = None
        depth = 0; start = -1
        in_str = False; escape_next = False
        for i, ch in enumerate(ai_text):
            if escape_next:
                escape_next = False
                continue
            if in_str:
                if ch == '\\':
                    escape_next = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == '[':
                if depth == 0: start = i
                depth += 1
            elif ch == ']':
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = ai_text[start:i+1]
                    try:
                        parsed = _json.loads(candidate)
                        if isinstance(parsed, list):
                            issues = parsed
                            break  # erstes valides Top-Level-Array nehmen
                    except: pass
                    start = -1
        if issues is None:
            print(f"[Opus-Audit] kein parsebares JSON-Array gefunden: {ai_text[:300]}")
            return []
        print(f"[Opus-Audit] {len(issues)} Issue(s) gefunden")
        for issue in issues:
            print(f"  [{issue.get('severity','?')}] {issue.get('feld','?')}: "
                  f"{issue.get('aktuell','?')} → {issue.get('korrekt','-')} "
                  f"({issue.get('grund','')[:120]})")
        return issues
    except Exception as e:
        print(f"[Opus-Audit] fail: {e}")
        return []


def parse_optionale_belege(files):
    """
    Liest optionale Belege mit Claude Vision KI.
    Unterstützt PDFs und Bilder (JPG, PNG, WEBP, HEIC).
    """
    if not ANTHROPIC_KEY:
        return []

    WISO_PFADE = {
        'tel':  {'name':'Telefon & Internet', 'wiso':'Werbungskosten → Arbeitsmittel → Telefon & Internet', 'hint':'20% der Jahreskosten ansetzbar', 'icon':'📱'},
        'lapt': {'name':'Laptop / Tablet', 'wiso':'Werbungskosten → Arbeitsmittel → Computer', 'hint':'Anteilig wenn privat mitgenutzt; ab 952€ AfA', 'icon':'💻'},
        'fach': {'name':'Fachliteratur', 'wiso':'Werbungskosten → Sonstiges → Fachbücher', 'hint':'Bücher und Zeitschriften zum Beruf', 'icon':'📚'},
        'reini':{'name':'Reinigung extra', 'wiso':'Werbungskosten → Sonstiges → Reinigung Berufskleidung', 'hint':'Mit Beleg über Pauschale hinaus', 'icon':'🧴'},
        'bewer':{'name':'Bewerbungskosten', 'wiso':'Werbungskosten → Sonstiges → Bewerbungskosten', 'hint':'Fahrtkosten, Bewerbungsmappen, Porto', 'icon':'💼'},
        'gew':  {'name':'Gewerkschaft / UFO', 'wiso':'Werbungskosten → Gewerkschaftsbeiträge', 'hint':'Voller Jahresbeitrag absetzbar', 'icon':'✊'},
        'stb':  {'name':'Steuerberatung', 'wiso':'Werbungskosten → Sonstiges → Steuerberatungskosten', 'hint':'Nur Werbungskosten-Anteil (BFH X R 10/08)', 'icon':'📋'},
        'bu':   {'name':'BU-Versicherung', 'wiso':'Vorsorgeaufwendungen → Sonstige Vorsorgeaufwendungen', 'hint':'Bis zum Höchstbetrag', 'icon':'🛡️'},
        'arzt': {'name':'Arztkosten', 'wiso':'Außergewöhnliche Belastungen → Krankheitskosten', 'hint':'Zumutbarkeitsgrenze beachten', 'icon':'🏥'},
        'zahn': {'name':'Zahnarzt', 'wiso':'Außergewöhnliche Belastungen → Krankheitskosten', 'hint':'Zumutbarkeitsgrenze beachten', 'icon':'🦷'},
        'fort': {'name':'Weiterbildung', 'wiso':'Werbungskosten → Fortbildungskosten', 'hint':'Voller Betrag absetzbar', 'icon':'🎓'},
        'arb':  {'name':'Arbeitsmittel', 'wiso':'Werbungskosten → Arbeitsmittel', 'hint':'Ab 952€ AfA beachten', 'icon':'🧳'},
        'hand': {'name':'Handwerkerleistungen', 'wiso':'Haushaltsnahe Dienstleistungen → Handwerkerleistungen', 'hint':'20% der Lohnkosten, max. 1.200€', 'icon':'🔧'},
        'haed': {'name':'Haushaltshilfe', 'wiso':'Haushaltsnahe Dienstleistungen', 'hint':'20% der Kosten, max. 4.000€', 'icon':'🧹'},
        'spen': {'name':'Spenden', 'wiso':'Sonderausgaben → Spenden und Mitgliedsbeiträge', 'hint':'Bis 20% der Einkünfte', 'icon':'💝'},
        'kind': {'name':'Kinderbetreuung', 'wiso':'Sonderausgaben → Kinderbetreuungskosten', 'hint':'2/3 der Kosten, max. 4.000€', 'icon':'👶'},
        'rv':   {'name':'Altersvorsorge', 'wiso':'Vorsorgeaufwendungen → Beiträge zur Altersvorsorge', 'hint':'Riester/Rürup Grenzen beachten', 'icon':'💰'},
        'haft': {'name':'Haftpflicht', 'wiso':'Vorsorgeaufwendungen → Sonstige', 'hint':'Anteilig absetzbar', 'icon':'⚖️'},
        'medi': {'name':'Medikamente', 'wiso':'Außergewöhnliche Belastungen → Krankheitskosten', 'hint':'Mit ärztlicher Verordnung', 'icon':'💊'},
        'konz': {'name':'Kontoführung', 'wiso':'Werbungskosten → Sonstige Werbungskosten', 'hint':'Pauschal 16€ oder Nachweis', 'icon':'🏦'},
        'kv':   {'name':'Krankenzusatz', 'wiso':'Vorsorgeaufwendungen → Sonstige', 'hint':'Anteilig', 'icon':'🦷'},
        'leb':  {'name':'Lebensversicherung', 'wiso':'Vorsorgeaufwendungen → Sonstige', 'hint':'Falls vor 2005', 'icon':'💚'},
        'haus': {'name':'Hausrat & Rechtsschutz', 'wiso':'Vorsorgeaufwendungen → Sonstige (nur beruflich)', 'hint':'Nur beruflicher Anteil (z.B. Berufsrechtsschutz)', 'icon':'🏠'},
        'pfle': {'name':'Pflege & Behinderung', 'wiso':'Außergewöhnliche Belastungen → Pflegekosten', 'hint':'Je nach Pflegegrad', 'icon':'🤝'},
        'under':{'name':'Unterhalt', 'wiso':'Außergewöhnliche Belastungen → Unterhalt', 'hint':'Max. 11.604€', 'icon':'👨‍👧'},
        'kata': {'name':'Außergewöhnl. Belastungen', 'wiso':'Außergewöhnliche Belastungen → Sonstige', 'hint':'Zumutbarkeitsgrenze', 'icon':'⛈️'},
        'part': {'name':'Partei-/Verbandsbeiträge', 'wiso':'Sonderausgaben → Parteibeiträge', 'hint':'Max. 1.650€', 'icon':'🏛️'},
    }

    results = []
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)

    def file_to_claude_content(file_bytes, filename=''):
        """Converts file bytes to Claude message content block(s)."""
        ext = filename.lower().split('.')[-1] if '.' in filename else ''
        b64 = base64.standard_b64encode(file_bytes).decode('utf-8')

        # JPEG / JPG
        if file_bytes[:3] == b'\xff\xd8\xff' or ext in ('jpg','jpeg'):
            return [{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':b64}}]
        # PNG
        if file_bytes[:8] == b'\x89PNG\r\n\x1a\n' or ext == 'png':
            return [{'type':'image','source':{'type':'base64','media_type':'image/png','data':b64}}]
        # WEBP
        if file_bytes[8:12] == b'WEBP' or ext == 'webp':
            return [{'type':'image','source':{'type':'base64','media_type':'image/webp','data':b64}}]
        # PDF — zuerst Text extrahieren, bei Misserfolg als Dokument senden
        if file_bytes[:4] == b'%PDF' or ext == 'pdf':
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    text = ' '.join(p.extract_text() or '' for p in pdf.pages)
                    if text.strip():
                        return [{'type':'text','text':text[:6000]}]
            except:
                pass
            return [{'type':'document','source':{'type':'base64','media_type':'application/pdf','data':b64}}]
        # HEIC/HEIF von iPhone — als JPEG konvertieren wenn PIL verfügbar
        if ext in ('heic','heif'):
            if PIL_AVAILABLE:
                try:
                    from PIL import Image as PILImage
                    img = PILImage.open(io.BytesIO(file_bytes))
                    buf = io.BytesIO()
                    img.convert('RGB').save(buf, format='JPEG', quality=85)
                    b64j = base64.standard_b64encode(buf.getvalue()).decode('utf-8')
                    return [{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':b64j}}]
                except:
                    pass
            return [{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':b64}}]
        # Unbekannt: versuche PDF-Text, dann JPEG
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = ' '.join(p.extract_text() or '' for p in pdf.pages)
                if text.strip():
                    return [{'type':'text','text':text[:6000]}]
        except:
            pass
        return [{'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':b64}}]

    for key, info in WISO_PFADE.items():
        if not files.get(key):
            continue

        content_blocks = []
        file_tuples = _bytes_filename_list(files[key])
        n_files = len(file_tuples)
        for file_bytes, filename in file_tuples:
            blocks = file_to_claude_content(file_bytes, filename)
            content_blocks.extend(blocks)

        if not content_blocks:
            continue

        content_blocks.append({
            'type': 'text',
            'text': f"""Du siehst {n_files} Beleg(e)/Rechnung(en) für: {info['name']}

Deine Aufgabe: Schätze den realistischen JAHRESGESAMTBETRAG für 2025.

Denke Schritt für Schritt:
1. Lies jeden Beleg — notiere Anbieter, Betrag, Zeitraum
2. Mehrere Belege → addiere alle
3. Fehlende Monate → schließe aus den vorhandenen:
   - Nur 1 Monat vorhanden → × 12 für ganzes Jahr
   - Mehrere Monate selber Preis → Durchschnitt × 12
   - Preisänderung erkennbar → jeden Zeitraum separat berechnen
   - Lücken zwischen zwei Anbietern → aus Nachbarmonaten schätzen
4. Gib den geschätzten Jahresbetrag an

Beispiele:
- Nur Dezember 39€ → 39×12=468 → betrag: 468.00, beschreibung: "Geschätzt auf Basis Dez 39€×12"
- Jan-Jun 32€, Jul-Dez 28€ → 192+168=360 → betrag: 360.00
- Jan-März 45€/Monat, Apr-Dez fehlt → 45×12=540 → betrag: 540.00
- Jahresrechnung 480€ → betrag: 480.00

Antworte NUR mit JSON, keine Backticks:
{{"betrag": 468.00, "zeitraum": "2025", "beschreibung": "Geschätzt auf Basis vorhandener Belege"}}

Wenn absolut kein Betrag erkennbar: {{"betrag": 0}}"""
        })

        try:
            response = client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=400,
                messages=[{'role': 'user', 'content': content_blocks}]
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r'```json|```', '', raw).strip()
            # Brace-counter JSON-Extraktion für Robustheit
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                m = re.search(r'\{[^{}]*"betrag"[^{}]*\}', raw, re.DOTALL)
                parsed = json.loads(m.group(0)) if m else {}
            betrag_raw = float(parsed.get('betrag', 0) or 0)
            beschreibung = parsed.get('beschreibung', '')

            # ── OPUS-VISION FALLBACK wenn Sonnet keinen Betrag rauskriegt ──
            if betrag_raw == 0:
                try:
                    print(f"[opus-vision] Sonnet got 0 for {key}, trying Opus + Vision...")
                    opus_resp = client.messages.create(
                        model='claude-opus-4-7',
                        max_tokens=600,
                        messages=[{'role': 'user', 'content': content_blocks[:-1] + [{
                            'type':'text',
                            'text': f"""Du bist erfahrener Werbungskosten-Klassifikator (branchenübliche Steuer-Praxis). Lies diese{'n' if n_files==1 else ''} {n_files} Beleg(e) für: {info['name']}.

Sonnet konnte keinen Betrag rausziehen. Versuch es nochmal — schaue GENAU auf:
- Stempel, handgeschriebene Zahlen
- "Gesamt", "Summe", "Brutto", "zu zahlen"
- Bei Telefonrechnung: monatliche Grundgebühr × 12 schätzen
- Bei Bild/Foto: jede Zahl die wie ein Geldbetrag aussieht

Wenn du WIRKLICH gar nichts findest aber das Dokument klar erkennbar ist (z.B. Telefon-Anbieter
sichtbar): schätz REALISTISCH (Telefon Standard 20-50€/Monat = 240-600€/Jahr;
Gewerkschaft 200-400€/Jahr; BU 500-1500€/Jahr).

JSON-Antwort, nichts anderes:
{{"betrag": 240.00, "zeitraum": "2025", "beschreibung": "Geschätzt: ..."}}"""
                        }]}]
                    )
                    opus_raw = opus_resp.content[0].text.strip()
                    opus_raw = re.sub(r'```json|```', '', opus_raw).strip()
                    try:
                        opus_parsed = json.loads(opus_raw)
                    except json.JSONDecodeError:
                        m = re.search(r'\{[^{}]*"betrag"[^{}]*\}', opus_raw, re.DOTALL)
                        opus_parsed = json.loads(m.group(0)) if m else {}
                    opus_betrag = float(opus_parsed.get('betrag', 0) or 0)
                    if opus_betrag > 0:
                        betrag_raw = opus_betrag
                        beschreibung = (opus_parsed.get('beschreibung') or '') + ' (Opus-Vision)'
                        print(f"[opus-vision] {key}: extracted {betrag_raw}€")
                except Exception as _ev:
                    print(f"[opus-vision] {key} fail: {_ev}")

            # ── Letzter Pauschale-Fallback wenn auch Opus nichts findet ──
            if betrag_raw == 0:
                if key == 'tel':
                    betrag_raw = 240.0
                    beschreibung = 'Pauschale 20€/Monat (R 9.1 Abs. 5 LStR + BFH 11.10.2007) — Beleg konnte auch Opus nicht lesen'
                elif key == 'konz':
                    betrag_raw = 16.0
                    beschreibung = 'Kontoführungs-Pauschale 16€/Jahr (BFH 09.05.1984)'
                else:
                    beschreibung = 'Betrag konnte nicht extrahiert werden — bitte Beleg manuell prüfen'
            results.append({
                'key': key,
                'icon': info['icon'],
                'name': info['name'],
                'wiso': info['wiso'],
                'hint': info['hint'],
                'betrag': betrag_raw,
                'zeitraum': parsed.get('zeitraum', '2025'),
                'beschreibung': beschreibung,
                'file_bytes_list': files[key],
            })
        except Exception as e:
            print(f'Optional doc {key} error: {e}')
            # Bei kompletten Parser-Fehlern: Pauschale-Fallback wo legitim
            fallback_betrag = 0.0
            fallback_desc = 'Betrag konnte nicht extrahiert werden'
            if key == 'tel':
                fallback_betrag = 240.0
                fallback_desc = 'Pauschale 20€/Monat (BFH 11.10.2007) — Beleg unleserlich'
            elif key == 'konz':
                fallback_betrag = 16.0
                fallback_desc = 'Pauschale (BFH 09.05.1984)'
            results.append({
                'key': key, 'icon': info['icon'], 'name': info['name'],
                'wiso': info['wiso'], 'hint': info['hint'],
                'betrag': fallback_betrag, 'zeitraum': '2025',
                'beschreibung': fallback_desc,
                'file_bytes_list': files[key],
            })

    return results



def infer_missing_data_with_ki(files, available_data, missing, parsed_summary=None):
    """
    When documents are missing or incomplete, Claude infers values
    from available documents. Always tries to be accurate using cross-references.
    Returns dict with inferred values and notes about what was estimated.

    parsed_summary: dict mit bereits korrekt geparsten Werten (z.B. SE-Monate, summen)
    damit Claude darüber NICHT editorialisiert.
    """
    if not ANTHROPIC_KEY:
        return {}, []

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
    notes = []  # Will be shown in PDF as warnings
    inferred = {}

    # Build context from available data — größere Limits, Claude verkraftet das problemlos
    context_parts = []

    if available_data.get('lsb_text'):
        context_parts.append(f"LOHNSTEUERBESCHEINIGUNG:\n{available_data['lsb_text'][:8000]}")
    if available_data.get('se_text'):
        context_parts.append(f"STRECKENEINSATZ-ABRECHNUNGEN — KOMPLETTER TEXT:\n{available_data['se_text'][:60000]}")
    if available_data.get('dp_text'):
        context_parts.append(f"FLUGSTUNDEN-ÜBERSICHTEN — KOMPLETTER TEXT:\n{available_data['dp_text'][:60000]}")

    if not context_parts:
        return {}, ['Zu wenige Dokumente für Schätzung vorhanden.']

    context = '\n\n'.join(context_parts)

    # Bereits korrekt geparste Werte — Claude soll DARÜBER nicht editorialisieren
    parsed_block = ''
    if parsed_summary:
        parsed_lines = [f"- {k}: {v}" for k, v in parsed_summary.items() if v]
        if parsed_lines:
            parsed_block = '\n\nDIESE WERTE WURDEN BEREITS KORREKT GEPARST und sind verlässlich — übernimm sie unverändert, schätze sie NICHT neu, schreibe KEINE Notes darüber:\n' + '\n'.join(parsed_lines)

    missing_str = ', '.join(missing)

    prompt = f"""Du bist ein Steuerexperte für Lufthansa-Flugbegleiter.

Folgende Dokumente sind VORHANDEN (kompletter Text — nicht abgeschnitten, alle Monate die du siehst sind tatsächlich da):
{context}{parsed_block}

Folgendes FEHLT oder konnte nicht gelesen werden: {missing_str}

WICHTIG zu Notes:
- Schreibe Notes AUSSCHLIESSLICH über die Items in der FEHLT-Liste oben.
- Keine Kommentare über bereits korrekt geparste Werte.
- Keine Aussagen wie "X Monate fehlen" wenn X nicht aus deiner Schätzung resultiert.
- Wenn ein Item NICHT in der FEHLT-Liste ist, ignoriere es bei den Notes komplett.

Bitte schätze die fehlenden Werte SO GENAU WIE MÖGLICH aus den vorhandenen Daten:

Regeln:
1. Wenn Monate in Streckeneinsatz fehlen → Durchschnitt der vorhandenen Monate × fehlende Anzahl
2. Wenn Flugstunden fehlen → aus Streckeneinsatz-Daten Arbeitstage/Nächte ableiten
3. Wenn LSB fehlt → Z17 auf 0 setzen (konservativ), Brutto aus Gehaltsstufe schätzen falls erkennbar
4. Wenn VMA-Ausland nicht berechenbar → aus Streckeneinsatz-Destinationen ableiten
5. Immer: lieber unterschätzen als überschätzen

Antworte NUR mit JSON (keine Backticks):
{{
  "arbeitstage": 133,
  "fahr_tage": 58,
  "hotel_naechte": 66,
  "vma_72_tage": 5,
  "vma_73_tage": 11,
  "vma_74_tage": 1,
  "vma_72": 70.0,
  "vma_73": 154.0,
  "vma_74": 28.0,
  "vma_aus": 4794.0,
  "km": 28,
  "spesen_gesamt": 5715.0,
  "spesen_steuer": 635.2,
  "z77": 5079.8,
  "ag_z17": 330.0,
  "brutto": 52884.81,
  "lohnsteuer": 7667.0,
  "abrechnungen": [],
  "notes": ["Monat März fehlte — aus Durchschnitt der anderen 11 Monate geschätzt"]
}}"""

    try:
        response = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=1000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        raw = re.sub(r'```json|```', '', response.content[0].text.strip()).strip()
        data = json.loads(raw)
        notes = data.pop('notes', [f'Fehlende Daten ({missing_str}) wurden aus vorhandenen Dokumenten geschätzt.'])
        inferred = data
        print(f"Inference successful: {list(inferred.keys())}")

        # ── OPUS-VERIFY bei hohem Z76 (>4000€) ────────────────────
        # Bei großen Auslandsspesen-Beträgen schickt ein zweiter Pass das Sonnet-Ergebnis
        # samt Originaltext zu Opus zur Plausibilitäts-Prüfung. Korrekturen werden übernommen.
        try:
            vma_aus_val = float(inferred.get('vma_aus', 0) or 0)
        except: vma_aus_val = 0.0
        if vma_aus_val > 4000:
            print(f"[opus-verify] Z76={vma_aus_val:.2f}€ > 4000 — Opus-Pass startet")
            verify_prompt = (
                "Du bist erfahrener Werbungskosten-Klassifikator (branchenübliche Steuer-Praxis). Sonnet hat aus den unten stehenden LH-Dokumenten "
                "diese Werte geschätzt. Bei großen Z76-Werten (>4000€) ist eine Zweit-Verifikation Pflicht.\n\n"
                f"SONNET-ERGEBNIS:\n{json.dumps(data, ensure_ascii=False, indent=2)}\n\n"
                f"DOKUMENTE (gleicher Kontext wie Sonnet):\n{context[:80000]}\n\n"
                "Prüfe Z76 (vma_aus) auf Plausibilität — passt der Wert zur Anzahl/Distanz der "
                "Auslandsdestinationen aus den Dokumenten? Sind 24h-Tage, An-/Abreise-Tage korrekt "
                "klassifiziert? BMF-Sätze realistisch?\n\n"
                "Antworte NUR mit JSON:\n"
                '{"vma_aus_correct": true/false, "vma_aus_corrected": <Zahl|null>, '
                '"reason": "kurze Begründung", "vma_72_corrected": <Zahl|null>, '
                '"vma_73_corrected": <Zahl|null>, "vma_74_corrected": <Zahl|null>}'
            )
            try:
                vresp = client.messages.create(
                    model='claude-opus-4-7', max_tokens=600,
                    messages=[{'role':'user','content': verify_prompt}]
                )
                vraw = re.sub(r'```json|```', '', vresp.content[0].text.strip()).strip()
                vdata = json.loads(vraw)
                if vdata.get('vma_aus_correct') is False:
                    corrected = vdata.get('vma_aus_corrected')
                    if isinstance(corrected, (int, float)) and corrected > 0:
                        inferred['vma_aus'] = float(corrected)
                        notes.append(f"Opus-Verify hat Z76 korrigiert ({vma_aus_val:.0f}€ → {corrected:.0f}€): {vdata.get('reason','')[:160]}")
                    for fld in ('vma_72', 'vma_73', 'vma_74'):
                        cor = vdata.get(f'{fld}_corrected')
                        if isinstance(cor, (int, float)) and cor >= 0:
                            inferred[fld] = float(cor)
                else:
                    print(f"[opus-verify] Z76 bestätigt: {vdata.get('reason','')[:120]}")
            except Exception as ve:
                print(f"[opus-verify] failed: {ve} — Sonnet-Werte bleiben")
    except Exception as e:
        print(f'Inference error: {e}')
        notes = [f'Schätzung fehlgeschlagen für: {missing_str}']

    return inferred, notes


# ════════════════════════════════════════════════════════════════════════
# HYBRID-ANALYSE: Sonnet (LSB+SE-Summen) + Opus (Tag-Klassifikation)
# Drei parallele KI-Calls, dann Merge. Single-Source-of-Truth pro Bereich.
# ════════════════════════════════════════════════════════════════════════

# v10.4.1: Parser-Versionen für File-Hash-Cache-Invalidierung.
# Bei Logik-Änderungen am Reader die entsprechende Version bumpen.
_LSB_PARSER_VERSION = 'v10.4.1'
_DP_PARSER_VERSION = 'v10.4.1'

# v11 Phase 3 — CAS-Reader-Konstanten
_CAS_PARSER_VERSION = 'v11.0.0'
_CAS_ACTIVITY_TYPES = (
    'flight',      # Tour mit LH-Flugnummer
    'office',      # ORTSTAG, FRS, LMN_AS, LMN_CR, Bürodienst
    'training',    # EH, EMCRM, SECCRM, TK, D4, EM (Schulungen)
    'simulator',   # SIM
    'standby',     # RES_SB, RES, SBY, Standby-zuhause oder am Airport
    'vacation',    # U, U1, U2, URLAUB
    'sick',        # K, KRANK
    'free',        # OFF, X, FREI
    'unknown',     # Fallback wenn nicht eindeutig erkennbar
)


def _parse_lsb_local_fast(pdf_bytes):
    """v10.4.1: Schneller lokaler LSB-Parser via pdfplumber + Regex.
    Standardisiertes eLSTB-Format hat strikte Zeilen-Nummerierung — keine KI nötig.

    Liefert dict mit allen Standard-Feldern WENN brutto + lohnsteuer sicher
    erkannt. Sonst None → Sonnet-Fallback.

    Critical fields für die Berechnung:
      - brutto (Zeile 3) — Pflicht
      - lohnsteuer (Zeile 4)
      - ag_fahrt_z17 (Zeile 17) → Anreisekosten-Topf
      - verpflegungszuschuss_z20 (Zeile 20) → wird vom Code nicht primär genutzt
    """
    import pdfplumber as _pp, io as _io, re as _re
    if not pdf_bytes:
        return None
    try:
        with _pp.open(_io.BytesIO(pdf_bytes)) as pdf:
            text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
    except Exception:
        return None

    # eLSTB-Signaturen — sonst kein gültiges LSB
    text_lc = text.lower()
    if not any(sig in text_lc for sig in [
        'lohnsteuerbescheinigung', 'eltb', 'elstb', 'elektronische lohnsteuer',
        'bruttoarbeitslohn',
    ]):
        return None

    def _eur(s):
        try:
            return float(str(s).replace('.', '').replace(',', '.'))
        except Exception:
            return None

    def _line_value(pattern, default=0.0):
        """Sucht Zeilen-Muster im Text. Toleriert verschiedene Whitespaces."""
        m = _re.search(pattern, text, _re.MULTILINE)
        if not m:
            return default
        v = _eur(m.group(1))
        return v if v is not None else default

    # eLSTB-Patterns — Zahlen typisch in Format „1.234,56" oder „52.884,81"
    # Zeile 3: Bruttoarbeitslohn — Pflicht
    # Format-Varianten: "3.    52.884,81" oder "3 52.884,81" oder mit "EUR"
    brutto_patterns = [
        r'(?:^|\n)\s*3\.?\s+(\d{1,3}(?:[.\s]?\d{3})*,\d{2})',
        r'Bruttoarbeitslohn[^\d\n]*(\d{1,3}(?:[.\s]?\d{3})*,\d{2})',
        r'\b3\.\s*Bruttoarbeitslohn[^\d]*?(\d{1,3}(?:[.\s]?\d{3})*,\d{2})',
    ]
    brutto = None
    for pat in brutto_patterns:
        m = _re.search(pat, text)
        if m:
            v = _eur(m.group(1))
            if v and v >= 1000:  # Sanity: Jahresbrutto mindestens 1000€
                brutto = v
                break
    if not brutto:
        return None  # Kein Brutto → kein verlässliches LSB → Sonnet

    result = {
        'brutto': brutto,
        'lohnsteuer': _line_value(r'(?:^|\n)\s*4\.?\s+(\d[\d.,\s]*)'),
        'soli': _line_value(r'(?:^|\n)\s*5\.?\s+(\d[\d.,\s]*)'),
        'kirchensteuer_an': _line_value(r'(?:^|\n)\s*6\.?\s+(\d[\d.,\s]*)'),
        'ag_fahrt_z17': _line_value(r'(?:^|\n)\s*17\.?\s+(\d[\d.,\s]*)'),
        'ag_fahrt_z18_pauschal': _line_value(r'(?:^|\n)\s*18\.?\s+(\d[\d.,\s]*)'),
        'verpflegungszuschuss_z20': _line_value(r'(?:^|\n)\s*20\.?\s+(\d[\d.,\s]*)'),
        'doppelhaus_z21': _line_value(r'(?:^|\n)\s*21\.?\s+(\d[\d.,\s]*)'),
        'rv_ag': _line_value(r'(?:^|\n)\s*22\s*a\.?\s+(\d[\d.,\s]*)'),
        'rv_an': _line_value(r'(?:^|\n)\s*23\s*a\.?\s+(\d[\d.,\s]*)'),
        'kv_an': _line_value(r'(?:^|\n)\s*25\.?\s+(\d[\d.,\s]*)'),
        'pv_an': _line_value(r'(?:^|\n)\s*26\.?\s+(\d[\d.,\s]*)'),
        'av_an': _line_value(r'(?:^|\n)\s*27\.?\s+(\d[\d.,\s]*)'),
        # Personalien — local hat keine echte Sicherheit, Sonnet liefert das im Fallback
        'identnr': '', 'geburtsdatum': '', 'personalnummer': '',
        'steuerklasse': '1', 'kinderfreibetraege': 0.0,
        'kirchensteuermerkmale': '', 'arbeitgeber': 'Deutsche Lufthansa AG',
        'finanzamt': '', 'steuernummer_ag': '',
        'vorsorge_gesamt_an': 0.0, 'rv_gesamt': 0.0,
        '_source': 'local_fast_v10.4.1',
        '_confidence': 'high',
    }

    # Sanity: lohnsteuer sollte ungefähr 15-35% vom Brutto sein
    if result['lohnsteuer'] > brutto * 0.5 or result['lohnsteuer'] < 0:
        return None  # Unplausibel → Sonnet

    print(f"[LSB-LocalFast] brutto={brutto:.2f} ls={result['lohnsteuer']:.2f} "
          f"z17={result['ag_fahrt_z17']:.2f} z20={result['verpflegungszuschuss_z20']:.2f} "
          f"(kein Sonnet-Call)")
    return result


# ────────────────────────────────────────────────────────────────────────────
# Task A2 — AI-gated LSB Fast-Reader (default: gated)
#
# Modus über AEROTAX_LSB_FAST_READER_MODE konfigurierbar:
#   off    → immer Sonnet (Pre-v10.4.1 Verhalten)
#   gated  → local nur wenn ALLE Confidence-Checks high (DEFAULT)
#   on     → local-first, Sonnet nur bei explizitem Low-Confidence
#
# Kernprinzip: Kein stilles Zero für Z17. Sicher 0 (Layout-Standard + kein
# Pattern-Match) ist OK; unsicher (Layout-Issue oder Multi-Match) → Sonnet.
# ────────────────────────────────────────────────────────────────────────────

def _aerotax_lsb_mode():
    """Liest aktuellen Modus zur Laufzeit (ENV kann sich ändern, nicht cachen)."""
    v = (os.environ.get('AEROTAX_LSB_FAST_READER_MODE', 'gated') or '').lower().strip()
    if v in ('off', 'gated', 'on'):
        return v
    return 'gated'  # Default bei ungültigem Wert


def _check_lsb_standard_layout(text):
    """Prüft ob ein Text-Layer einer Standard-eLSTB entspricht.
    Returns dict mit confidence + red_flags."""
    if not text:
        return {'confidence': 'low', 'red_flags': ['no_text_layer']}
    text_lc = text.lower()

    # Pflicht-Signaturen
    has_lsb_title = any(s in text_lc for s in [
        'lohnsteuerbescheinigung', 'elektronische lohnsteuer', 'elstb', 'eltb',
    ])
    has_brutto_keyword = 'bruttoarbeitslohn' in text_lc
    # Mindestens 5 der Standard-Zeilennummern (3-27 typisch) muss erkennbar sein
    import re as _re
    visible_lines = set()
    for m in _re.finditer(r'(?:^|\n)\s*(\d{1,2})\.?\s+', text):
        try:
            n = int(m.group(1))
            if 1 <= n <= 35:
                visible_lines.add(n)
        except Exception:
            pass
    line_coverage = len(visible_lines & {3, 4, 5, 17, 22, 23, 25, 26, 27})

    red_flags = []
    if not has_lsb_title:
        red_flags.append('no_lsb_title')
    if not has_brutto_keyword:
        red_flags.append('no_bruttoarbeitslohn')
    if line_coverage < 4:
        red_flags.append(f'few_standard_lines_visible({line_coverage}/9)')
    # Indikatoren für gescanntes/rotiertes Dokument
    if len(text.strip()) < 200:
        red_flags.append('very_little_text')

    if not red_flags and has_lsb_title and has_brutto_keyword and line_coverage >= 6:
        conf = 'high'
    elif len(red_flags) == 1 and line_coverage >= 5:
        conf = 'medium'
    else:
        conf = 'low'

    return {
        'confidence': conf,
        'red_flags': red_flags,
        'has_lsb_title': has_lsb_title,
        'has_brutto_keyword': has_brutto_keyword,
        'visible_lines_count': line_coverage,
    }


def _extract_lsb_field_with_evidence(text, line_num, allow_absent=True):
    """Extrahiert eine Zeile (z.B. Z17) mit Confidence + Evidence.

    eLSTB-Format ist tabellarisch: „17. <Bezeichnung>            <Wert>".
    Strategie: Zeilen finden die mit Zeilennummer beginnen, dann LAST EUR-Wert
    in der Zeile. Mehrere Zeilen → conflict (außer identische Werte).

    Returns dict:
      - value: float oder None
      - confidence: 'high' / 'medium' / 'low' / 'conflict'
      - evidence: dict mit raw_line/candidates/reason
      - definitely_absent: True wenn Layout-Standard aber kein Match (= sicher 0)
    """
    import re as _re

    def _eur(s):
        try:
            return float(str(s).replace('.', '').replace(',', '.'))
        except Exception:
            return None

    # Zeilen finden die mit Zeilennummer beginnen (z.B. "17." oder "17 ")
    line_start_pat = rf'^\s*{line_num}\.?\s+.+$'
    matching_lines = _re.findall(line_start_pat, text, _re.MULTILINE)

    # EUR-Werte (deutsches Format mit Komma) — pro Zeile suchen
    num_pat = r'(\d{1,3}(?:[.\s]?\d{3})*,\d{2})'

    candidates = []  # list of (value, raw_line) tuples
    for line in matching_lines:
        nums_in_line = _re.findall(num_pat, line)
        if not nums_in_line:
            continue
        # eLSTB-Konvention: letzte Zahl pro Zeile = Wert (Text dazwischen ist Bezeichnung)
        val = _eur(nums_in_line[-1])
        if val is not None:
            candidates.append((val, line.strip()))

    if not candidates:
        if allow_absent:
            return {
                'value': 0.0, 'confidence': 'high',
                'definitely_absent': True,
                'evidence': {'reason': f'line_{line_num}_not_found_in_standard_layout'},
            }
        return {
            'value': None, 'confidence': 'low',
            'definitely_absent': False,
            'evidence': {'reason': f'line_{line_num}_unreadable'},
        }

    values = [c[0] for c in candidates]
    if len(set(values)) > 1:
        # Mehrere Zeilen mit DIFFERENTEN Werten → conflict
        return {
            'value': None, 'confidence': 'conflict',
            'definitely_absent': False,
            'evidence': {'reason': f'line_{line_num}_multiple_candidates',
                          'candidates': values},
        }

    # Single value (oder mehrere identische) → high
    return {
        'value': values[0], 'confidence': 'high',
        'definitely_absent': False,
        'evidence': {'reason': f'line_{line_num}_match',
                      'raw': candidates[0][1][:80],
                      'match_count': len(candidates)},
    }


def _parse_lsb_local_with_confidence(pdf_bytes):
    """Confidence-aware LSB-Reader. Ergänzt _parse_lsb_local_fast um pro-Feld
    Confidence + Evidence. Wird im 'gated' Modus genutzt — strikte Checks
    bevor das lokale Ergebnis vertraut wird.

    Returns dict oder None (bei Crash/leerer Input).
    """
    import pdfplumber as _pp, io as _io
    if not pdf_bytes:
        return None
    try:
        with _pp.open(_io.BytesIO(pdf_bytes)) as pdf:
            text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
    except Exception:
        return None

    layout = _check_lsb_standard_layout(text)
    if layout['confidence'] == 'low':
        # Layout nicht erkennbar → lokal nicht sinnvoll
        return {
            'overall_confidence': 'low',
            'layout_check': layout,
            'reason': 'non_standard_layout',
        }

    # Felder mit Evidence
    brutto = _extract_lsb_field_with_evidence(text, 3, allow_absent=False)
    if brutto['value'] is None or brutto['value'] < 1000:
        return {
            'overall_confidence': 'low',
            'layout_check': layout,
            'brutto': brutto,
            'reason': 'brutto_missing_or_implausible',
        }

    lohnsteuer = _extract_lsb_field_with_evidence(text, 4, allow_absent=True)
    soli = _extract_lsb_field_with_evidence(text, 5, allow_absent=True)
    kirche = _extract_lsb_field_with_evidence(text, 6, allow_absent=True)
    z17 = _extract_lsb_field_with_evidence(text, 17, allow_absent=True)
    z18 = _extract_lsb_field_with_evidence(text, 18, allow_absent=True)
    z20 = _extract_lsb_field_with_evidence(text, 20, allow_absent=True)
    z21 = _extract_lsb_field_with_evidence(text, 21, allow_absent=True)

    # Sanity: lohnsteuer plausibel?
    ls_val = lohnsteuer.get('value') or 0
    if ls_val > brutto['value'] * 0.5:
        return {
            'overall_confidence': 'low',
            'layout_check': layout,
            'reason': 'lohnsteuer_implausible',
            'brutto': brutto, 'lohnsteuer': lohnsteuer,
        }

    # Overall-Confidence: nur high wenn ALLES high
    critical_fields = [brutto, lohnsteuer, z17]
    if all(f['confidence'] == 'high' for f in critical_fields):
        overall = 'high'
    elif any(f['confidence'] == 'conflict' for f in [brutto, lohnsteuer, z17, z18, z20, z21]):
        overall = 'conflict'
    elif any(f['confidence'] == 'low' for f in critical_fields):
        overall = 'low'
    else:
        overall = 'medium'

    return {
        'overall_confidence': overall,
        'layout_check': layout,
        'brutto': brutto,
        'lohnsteuer': lohnsteuer,
        'soli': soli,
        'kirchensteuer_an': kirche,
        'ag_fahrt_z17': z17,
        'ag_fahrt_z18_pauschal': z18,
        'verpflegungszuschuss_z20': z20,
        'doppelhaus_z21': z21,
    }


def _flatten_local_to_lsb_dict(local_result):
    """Konvertiert confidence-aware Result zu flachem LSB-Dict (Format wie _sonnet_read_lsb_v2)."""
    def _v(field):
        if not isinstance(field, dict): return 0.0
        return float(field.get('value') or 0.0)
    out = {
        'brutto': _v(local_result.get('brutto')),
        'lohnsteuer': _v(local_result.get('lohnsteuer')),
        'soli': _v(local_result.get('soli')),
        'kirchensteuer_an': _v(local_result.get('kirchensteuer_an')),
        'ag_fahrt_z17': _v(local_result.get('ag_fahrt_z17')),
        'ag_fahrt_z18_pauschal': _v(local_result.get('ag_fahrt_z18_pauschal')),
        'verpflegungszuschuss_z20': _v(local_result.get('verpflegungszuschuss_z20')),
        'doppelhaus_z21': _v(local_result.get('doppelhaus_z21')),
        'rv_ag': 0.0, 'rv_an': 0.0, 'kv_an': 0.0, 'pv_an': 0.0, 'av_an': 0.0,
        'identnr': '', 'geburtsdatum': '', 'personalnummer': '',
        'steuerklasse': '1', 'kinderfreibetraege': 0.0,
        'kirchensteuermerkmale': '', 'arbeitgeber': 'Deutsche Lufthansa AG',
        'finanzamt': '', 'steuernummer_ag': '',
        'vorsorge_gesamt_an': 0.0, 'rv_gesamt': 0.0,
        '_source': 'local_gated_v10.4.2',
        '_confidence': local_result.get('overall_confidence', 'medium'),
        '_audit': {
            'layout': local_result.get('layout_check', {}),
            'z17_evidence': local_result.get('ag_fahrt_z17', {}).get('evidence'),
            'brutto_evidence': local_result.get('brutto', {}).get('evidence'),
        },
    }
    return out


# Backwards-Compat: alte Konstante bleibt erhalten für bestehende Tests
_AEROTAX_LSB_LOCAL_FIRST = (os.environ.get('AEROTAX_LSB_LOCAL_FIRST', '') == '1')


def _read_lsb_with_local_fallback(pdf_bytes_list):
    """Task A2: LSB-Reader mit AI-gated Fast-Reader.

    AEROTAX_LSB_FAST_READER_MODE:
      off    → immer Sonnet
      gated  → local nur bei high-confidence Standard-Layout + eindeutigem Z17 (DEFAULT)
      on     → local mit Fallback bei Low-Confidence

    Schutz-Regeln:
      - Multi-LSB → immer Sonnet (Aggregation komplex)
      - Layout non-standard → Sonnet
      - Z17 mehrdeutig → Sonnet
      - Z17 unklar lesbar (kein definitely_absent) → Sonnet
      - Lohnsteuer implausibel → Sonnet
    """
    pdf_bytes_list = _bytes_list(pdf_bytes_list) if pdf_bytes_list else []
    if not pdf_bytes_list:
        return None

    mode = _aerotax_lsb_mode()

    # Multi-LSB: immer Sonnet
    if len(pdf_bytes_list) > 1:
        print(f"[LSB] Multi-LSB (n={len(pdf_bytes_list)}) → Sonnet")
        return _sonnet_read_lsb_v2(pdf_bytes_list)

    # Mode 'off' → direkt Sonnet
    if mode == 'off':
        return _sonnet_read_lsb_v2(pdf_bytes_list)

    # Mode 'gated' / 'on' → confidence-aware Reader probieren
    try:
        local = _parse_lsb_local_with_confidence(pdf_bytes_list[0])
    except Exception as e:
        print(f"[LSB] gated reader error: {str(e)[:120]} → Sonnet")
        return _sonnet_read_lsb_v2(pdf_bytes_list)

    if not local:
        print(f"[LSB] gated reader: kein Text-Layer → Sonnet")
        return _sonnet_read_lsb_v2(pdf_bytes_list)

    overall = local.get('overall_confidence')
    layout_conf = (local.get('layout_check') or {}).get('confidence')

    if mode == 'gated':
        # Strikte Checks
        z17 = local.get('ag_fahrt_z17', {}) or {}
        z17_ok = z17.get('confidence') == 'high'  # high inkl. definitely_absent

        if overall != 'high':
            print(f"[LSB] gated: overall_confidence={overall} ≠ high → Sonnet")
            return _sonnet_read_lsb_v2(pdf_bytes_list)
        if layout_conf != 'high':
            print(f"[LSB] gated: layout_confidence={layout_conf} ≠ high → Sonnet")
            return _sonnet_read_lsb_v2(pdf_bytes_list)
        if not z17_ok:
            print(f"[LSB] gated: z17.confidence={z17.get('confidence')} → Sonnet")
            return _sonnet_read_lsb_v2(pdf_bytes_list)

        result = _flatten_local_to_lsb_dict(local)
        result['_local_used_reason'] = 'gated_all_checks_high'
        print(f"[LSB] gated: alle Checks high — local used (brutto={result['brutto']:.2f}, z17={result['ag_fahrt_z17']:.2f})")
        return result

    if mode == 'on':
        # Lockerer — accept high und medium
        if overall == 'low':
            print(f"[LSB] mode=on: overall=low → Sonnet")
            return _sonnet_read_lsb_v2(pdf_bytes_list)
        result = _flatten_local_to_lsb_dict(local)
        result['_local_used_reason'] = f'mode_on_overall_{overall}'
        print(f"[LSB] mode=on: confidence={overall} — local used")
        return result

    # Unreachable defensive
    return _sonnet_read_lsb_v2(pdf_bytes_list)


def _sonnet_read_lsb_v2(pdf_bytes_list):
    """Sonnet liest LSB-PDF(s) direkt via Tool-Use. Standardisiertes Format
    der elektronischen LSB → Sonnet kann das deterministisch abklopfen.
    Bei Multi-LSB: numerische Werte werden addiert, Personalien von 1. PDF."""
    if not pdf_bytes_list or not ANTHROPIC_KEY:
        return None
    pdf_bytes_list = _bytes_list(pdf_bytes_list)

    accumulated = {
        'brutto': 0.0, 'lohnsteuer': 0.0, 'soli': 0.0,
        'kirchensteuer_an': 0.0, 'ag_fahrt_z17': 0.0, 'ag_fahrt_z18_pauschal': 0.0,
        'rv_an': 0.0, 'kv_an': 0.0, 'pv_an': 0.0, 'av_an': 0.0, 'rv_ag': 0.0,
        'verpflegungszuschuss_z20': 0.0, 'doppelhaus_z21': 0.0,
        'identnr': '', 'geburtsdatum': '', 'personalnummer': '',
        'steuerklasse': '1', 'kinderfreibetraege': 0.0,
        'kirchensteuermerkmale': '', 'arbeitgeber': 'Deutsche Lufthansa AG',
        'finanzamt': '', 'steuernummer_ag': '',
        'vorsorge_gesamt_an': 0.0, 'rv_gesamt': 0.0,
    }

    lsb_tool = {
        'name': 'submit_lsb_extraktion',
        'description': 'Extrahiere alle Felder aus der Lohnsteuerbescheinigung',
        'input_schema': {
            'type': 'object',
            'required': ['brutto'],
            'properties': {
                'brutto':           {'type': 'number', 'description': 'Bruttoarbeitslohn (Zeile 3)'},
                'lohnsteuer':       {'type': 'number', 'description': 'einbehaltene Lohnsteuer (Zeile 4)'},
                'soli':             {'type': 'number', 'description': 'Solidaritätszuschlag (Zeile 5)'},
                'kirchensteuer_an': {'type': 'number', 'description': 'Kirchensteuer (Zeile 6/7)'},
                'ag_fahrt_z17':     {'type': 'number', 'description': 'AG-Fahrkostenzuschuss Entfernungspauschale (Zeile 17)'},
                'ag_fahrt_z18_pauschal': {'type': 'number', 'description': 'Pauschalversteuert 15% (Zeile 18) — Jobticket-Indikator'},
                'verpflegungszuschuss_z20': {'type': 'number', 'description': 'stfrei Verpflegung (Zeile 20)'},
                'doppelhaus_z21':   {'type': 'number', 'description': 'Doppelter Haushalt (Zeile 21)'},
                'rv_ag':            {'type': 'number', 'description': 'AG-Rentenversicherung (Zeile 22a)'},
                'rv_an':            {'type': 'number', 'description': 'AN-Rentenversicherung (Zeile 23a)'},
                'kv_an':            {'type': 'number', 'description': 'AN-Krankenversicherung (Zeile 25)'},
                'pv_an':            {'type': 'number', 'description': 'AN-Pflegeversicherung (Zeile 26)'},
                'av_an':            {'type': 'number', 'description': 'AN-Arbeitslosenversicherung (Zeile 27)'},
                'identnr':          {'type': 'string', 'description': 'Steuerliche Identifikationsnummer (11 Ziffern)'},
                'geburtsdatum':     {'type': 'string', 'description': 'DD.MM.YYYY'},
                'personalnummer':   {'type': 'string'},
                'steuerklasse':     {'type': 'string', 'description': '1-6'},
                'kinderfreibetraege': {'type': 'number'},
                'kirchensteuermerkmale': {'type': 'string'},
                'arbeitgeber':      {'type': 'string'},
                'finanzamt':        {'type': 'string'},
                'steuernummer_ag':  {'type': 'string'},
            }
        }
    }

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
    is_first = True
    for pdf_bytes in pdf_bytes_list:
        try:
            content = [
                {
                    'type': 'document',
                    'source': {'type': 'base64', 'media_type': 'application/pdf',
                               'data': base64.b64encode(pdf_bytes).decode()}
                },
                {
                    'type': 'text',
                    'text': """Du bekommst eine deutsche elektronische Lohnsteuerbescheinigung (eLSTB).

═══ FORMAT EINER eLSTB ═══

Standard-Aufbau, jede Zeile hat eine Nummer:
  Zeile 3:  Bruttoarbeitslohn — Jahresbruttolohn
  Zeile 4:  Einbehaltene Lohnsteuer
  Zeile 5:  Solidaritätszuschlag
  Zeile 6:  Kirchensteuer Arbeitnehmer
  Zeile 7:  Kirchensteuer Ehegatte (selten relevant)
  Zeile 17: Steuerpflichtiger AG-Fahrkostenzuschuss (NICHT Z18!)
  Zeile 18: Pauschal versteuerter (15%) Fahrkostenzuschuss / Jobticket-Indikator
  Zeile 20: Steuerfreie Verpflegungszuschüsse
  Zeile 21: Steuerfreie Erstattung doppelter Haushalt
  Zeile 22a: Arbeitgeber-Anteil Rentenversicherung
  Zeile 23a: Arbeitnehmer-Anteil Rentenversicherung
  Zeile 25: Arbeitnehmer-Anteil Krankenversicherung
  Zeile 26: Arbeitnehmer-Anteil Pflegeversicherung
  Zeile 27: Arbeitnehmer-Anteil Arbeitslosenversicherung

═══ REGELN ═══

✓ Lies LITERAL — keine Schätzungen.
✓ Deutsche Zahlen umwandeln: '1.234,56 €' → 1234.56 (Punkt = Tausender, Komma = Dezimal)
✓ Wenn Feld leer / nicht vorhanden / 0,00 → liefere 0 (NICHT null/None)
✓ Personalien: nur was wirklich da steht, sonst leerer String
✓ Wenn unklar welche Spalte: Z17 (steuerpflichtig) ist normalerweise GRÖßER als Z18 (pauschal),
  und Z17 wird in der Regel direkt unter "Werbungskosten" gerechnet
✓ Identifikationsnummer: 11 Ziffern, formatiert wie 12345678901
✓ Geburtsdatum: DD.MM.YYYY

═══ MULTI-SEITEN ═══

Wenn die LSB mehrere Seiten hat: Werte stehen typisch auf Seite 1.
Manche Felder können auf Folgeseiten weitergehen — lies ALLE Seiten.

Liefere via Tool. Bei UNSICHER welcher Wert zu welcher Zeile gehört: lieber 0 als
falscher Wert."""
                }
            ]
            resp = None
            for attempt in range(3):
                try:
                    resp = client.messages.create(
                        model='claude-sonnet-4-6', max_tokens=2000,
                        tools=[lsb_tool],
                        tool_choice={'type': 'tool', 'name': 'submit_lsb_extraktion'},
                        messages=[{'role': 'user', 'content': content}]
                    )
                    break
                except Exception as e:
                    if attempt == 2: raise
                    print(f"[Sonnet-LSB] retry {attempt+1}/3: {str(e)[:100]}")
                    import time as _t; _t.sleep(2 ** attempt + 1)
            tool_input = None
            for block in resp.content:
                if getattr(block, 'type', None) == 'tool_use':
                    tool_input = block.input
                    break
            if not tool_input:
                print(f"[Sonnet-LSB] kein tool_input erhalten")
                continue

            # Numerische Werte addieren (Multi-LSB)
            for k in ['brutto','lohnsteuer','soli','kirchensteuer_an',
                      'ag_fahrt_z17','ag_fahrt_z18_pauschal',
                      'rv_an','kv_an','pv_an','av_an','rv_ag',
                      'verpflegungszuschuss_z20','doppelhaus_z21','kinderfreibetraege']:
                v = tool_input.get(k)
                try: accumulated[k] = round((accumulated[k] or 0) + float(v or 0), 2)
                except: pass
            # Personalien nur von 1. LSB
            if is_first:
                for k in ['identnr','geburtsdatum','personalnummer','steuerklasse',
                          'kirchensteuermerkmale','arbeitgeber','finanzamt','steuernummer_ag']:
                    v = tool_input.get(k)
                    if v: accumulated[k] = str(v)
                is_first = False
            print(f"[Sonnet-LSB] gelesen: brutto={accumulated['brutto']:.2f} z17={accumulated['ag_fahrt_z17']:.2f} z20={accumulated['verpflegungszuschuss_z20']:.2f}")
        except Exception as e:
            print(f"[Sonnet-LSB] PDF fail: {e}")

    accumulated['vorsorge_gesamt_an'] = round(
        accumulated['rv_an'] + accumulated['kv_an'] +
        accumulated['pv_an'] + accumulated['av_an'], 2)
    accumulated['rv_gesamt'] = round(accumulated['rv_an'] + accumulated['rv_ag'], 2)
    return accumulated


# ═════════════════════════════════════════════════════════════════════════════
# v6.0 STRUCTURED-DAY-PIPELINE
#
# Design-Prinzip: Sonnet liest Dienstplan/Einsatzplan strukturiert pro Tag aus.
# Das Backend zählt harte Fakten (Hotelnächte, Arbeitstage, Fahrtage) deterministisch.
# Opus darf diese Fakten nicht ändern, sondern nur steuerlich klassifizieren.
#
# Ablauf:
#   1. _sonnet_read_dp_structured() liefert tag_data[365]
#   2. _count_deterministic() berechnet hotel_naechte / arbeitstage / fahr_tage
#   3. _opus_classify_structured_days_v6() klassifiziert pro Tag (Z72/Z73/Z76/...)
#   4. _validate_opus_against_structure() prüft Opus gegen harte Fakten
# ═════════════════════════════════════════════════════════════════════════════

def _as_dict_item(item):
    """Zentrale Normalisierung für LLM-Output-Items.
    Akzeptiert: dict, tuple(key,value), pydantic-Model, Object mit __dict__.
    Liefert immer dict (leeres dict bei nicht konvertierbar)."""
    if isinstance(item, dict):
        return item
    if isinstance(item, tuple) and len(item) == 2:
        k, v = item
        if isinstance(v, dict):
            return {'datum': k, **v}
        return {'datum': k, 'value': v}
    if hasattr(item, 'model_dump'):
        try:
            return item.model_dump()
        except Exception:
            pass
    if hasattr(item, '__dict__'):
        try:
            return dict(item.__dict__)
        except Exception:
            pass
    return {}


# Alias für Rückwärts-Kompatibilität (v6.0.x verwendete _ensure_dict)
_ensure_dict = _as_dict_item


def _normalize_v6_classifications(raw):
    """Akzeptiert beliebige Eingabe-Strukturen und liefert immer list[dict] zurück.
    Edge-Cases die Anthropic-SDK manchmal liefert:
    - list[dict] (normal)
    - dict[datum → dict] (auch möglich)
    - dict mit key 'classifications'/'days' der Liste enthält
    - list[tuple(datum, dict)] (durch dict.items() irgendwo)
    - pydantic-Models statt dicts
    """
    if raw is None:
        return []

    # Wenn dict: prüfe gängige Wrapper-Keys
    if isinstance(raw, dict):
        if isinstance(raw.get('classifications'), list):
            raw = raw['classifications']
        elif isinstance(raw.get('days'), list):
            raw = raw['days']
        else:
            # dict[datum → dict] — keys sind die Daten
            raw = [
                {'datum': k, **v} if isinstance(v, dict) else {'datum': k, 'value': v}
                for k, v in raw.items()
            ]

    if not isinstance(raw, (list, tuple)):
        return []

    out = []
    for item in raw:
        if isinstance(item, tuple) and len(item) == 2:
            k, v = item
            if isinstance(v, dict):
                out.append({'datum': k, **v})
            else:
                out.append({'datum': k, 'value': v})
        elif isinstance(item, dict):
            out.append(item)
        else:
            # pydantic-Model oder anderes Object
            d = _ensure_dict(item)
            if d:
                out.append(d)
    return out


INLAND_IATA_CODES = {
    'FRA', 'MUC', 'HAM', 'DUS', 'STR', 'CGN', 'HAJ', 'BER', 'TXL', 'SXF',
    'LEJ', 'NUE', 'BRE', 'FMO', 'PAD', 'NRN', 'FKB', 'HHN', 'SCN', 'DRS',
    'ERF', 'FDH', 'RLG', 'KSF', 'XFW', 'EDDF', 'EDDM', 'EDDH', 'EDDL', 'EDDS',
}


def _is_inland_code(code):
    """True wenn IATA-/ICAO-Code Inland-Flughafen ist."""
    if not code:
        return False
    return code.upper().strip() in INLAND_IATA_CODES


# ════════════════════════════════════════════════════════════════════════════
# v11 Phase 3 — CAS / Dienstplan / Roster Main-Reader
#
# Liest CAS-PDFs strukturiert pro Tag. Ersetzt in v11 die Flugstundenübersicht
# als Tag-Aktivitäts-Quelle. Eine Sonnet-Anfrage pro PDF (memory-bounded),
# Cache via SHA-256 + parser_version. Multi-File-Merge mit Konflikt-Detection.
#
# Output-Schema pro Tag:
#   {date, activity_type, marker, start_time, end_time, duration_minutes,
#    location, flights[], overnight_after_day, layover_ort, confidence,
#    raw_excerpt, source_file_id, source_filename}
#
# Hybrid-Checks (zusätzlich zum Per-Tag-Schema):
#   - Cross-File-Dedupe: gleiche Datei zweimal → ignoriert
#   - Cross-Month-Konflikt: gleicher Tag in 2 Files mit anderer Activity → conflict
#   - Self-Consistency: activity_type passt zu Zeiten/Flügen
# ════════════════════════════════════════════════════════════════════════════


def _validate_cas_day(day):
    """Sanity-Check pro CAS-Tag.
    Returns (is_valid, normalized_day_dict, warning_or_None)."""
    if not isinstance(day, dict):
        return False, None, 'not_dict'
    date = day.get('date') or day.get('datum') or ''
    activity_type = (day.get('activity_type') or 'unknown').lower()
    if activity_type not in _CAS_ACTIVITY_TYPES:
        activity_type = 'unknown'
    # Datum-Validierung
    import re as _re
    if not _re.match(r'^\d{4}-\d{2}-\d{2}$', date):
        return False, None, f'invalid_date:{date}'

    out = {
        'date': date,
        'activity_type': activity_type,
        'marker': str(day.get('marker') or '')[:60],
        'start_time': str(day.get('start_time') or '')[:6],
        'end_time': str(day.get('end_time') or '')[:6],
        'duration_minutes': int(day.get('duration_minutes') or 0) if str(day.get('duration_minutes', '')).strip() else 0,
        'location': str(day.get('location') or '')[:10],
        'flights': day.get('flights') if isinstance(day.get('flights'), list) else [],
        'overnight_after_day': bool(day.get('overnight_after_day')),
        'layover_ort': str(day.get('layover_ort') or '')[:10],
        'confidence': (day.get('confidence') or 'medium').lower(),
        'raw_excerpt': str(day.get('raw_excerpt') or '')[:120],
    }
    if out['confidence'] not in ('high', 'medium', 'low'):
        out['confidence'] = 'medium'

    # Self-Consistency: activity_type passt zu Daten?
    warning = None
    if activity_type == 'flight' and not out['flights']:
        warning = f'{date}: activity=flight aber keine Flüge'
    elif activity_type in ('training', 'office', 'simulator') and not (out['start_time'] and out['end_time']):
        warning = f'{date}: activity={activity_type} aber Zeiten fehlen'
        # Downgrade confidence
        if out['confidence'] == 'high':
            out['confidence'] = 'medium'

    return True, out, warning


def _sonnet_read_cas_single_pdf(pdf_bytes, year, homebase, source_filename='cas.pdf'):
    """Liest EINE CAS-PDF via Sonnet Vision. Returns dict mit days[]+warnings[].
    Wird vom Multi-File-Wrapper _sonnet_read_cas_structured aufgerufen."""
    if not pdf_bytes or not ANTHROPIC_KEY:
        return None
    import base64 as _b64, json as _j, re as _re

    cas_tool = {
        'name': 'submit_cas_days',
        'description': 'Liefere strukturierte Tagesdaten aus dem CAS/Dienstplan/Roster.',
        'input_schema': {
            'type': 'object',
            'required': ['days'],
            'properties': {
                'days': {
                    'type': 'array',
                    'description': f'Pro Kalendertag im Plan ein Eintrag (auch frei/OFF). '
                                   f'Jahr {year}, Homebase {homebase}.',
                    'items': {
                        'type': 'object',
                        'required': ['date', 'activity_type'],
                        'properties': {
                            'date': {'type': 'string', 'description': 'YYYY-MM-DD'},
                            'activity_type': {
                                'type': 'string',
                                'enum': list(_CAS_ACTIVITY_TYPES),
                                'description': 'Aktivitäts-Typ. flight=LH-Flugnummer, '
                                                'training=EH/EMCRM/SECCRM/TK/D4/EM, '
                                                'office=ORTSTAG/FRS/LMN, simulator=SIM, '
                                                'standby=RES_SB/RES/SBY, vacation=U/U1/U2, '
                                                'sick=K, free=OFF/X, unknown=alles andere.',
                            },
                            'marker': {'type': 'string', 'description': 'Roh-Code wie er im CAS steht (z.B. „EH 4", „LH600", „U1", „RES_SB").'},
                            'start_time': {'type': 'string', 'description': 'HH:MM (24h) — Briefing/Aktivitäts-Start. Leer bei frei/Urlaub.'},
                            'end_time': {'type': 'string', 'description': 'HH:MM — letzte Flug-Landung oder Aktivitäts-Ende. Bei Tour über Mitternacht: bis 23:59 dieses Tags.'},
                            'duration_minutes': {'type': 'integer', 'description': 'Differenz end_time - start_time in Minuten.'},
                            'location': {'type': 'string', 'description': 'Briefing-Location IATA (meist FRA) oder Layover-Ort.'},
                            'flights': {
                                'type': 'array',
                                'description': 'Nur bei activity_type=flight. Liste der Flüge.',
                                'items': {
                                    'type': 'object',
                                    'properties': {
                                        'flight_no': {'type': 'string', 'description': 'z.B. „LH600"'},
                                        'from_iata': {'type': 'string', 'description': '3-Letter IATA'},
                                        'to_iata': {'type': 'string', 'description': '3-Letter IATA'},
                                        'start_time': {'type': 'string', 'description': 'HH:MM'},
                                        'end_time': {'type': 'string', 'description': 'HH:MM'},
                                    },
                                },
                            },
                            'overnight_after_day': {'type': 'boolean', 'description': 'Wahr wenn Tag in remote Location endet (Layover-Übernachtung).'},
                            'layover_ort': {'type': 'string', 'description': 'IATA-Code falls remote, leer wenn FRA-Heimkehr.'},
                            'confidence': {
                                'type': 'string',
                                'enum': ['high', 'medium', 'low'],
                                'description': 'high=klar lesbar, medium=teils unklar, low=sehr unsicher.',
                            },
                            'raw_excerpt': {'type': 'string', 'description': 'Kurzer Roh-Auszug aus CAS (max 80 Zeichen).'},
                        },
                    },
                },
                'warnings': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Lese-Warnungen (z.B. „Plan unvollständig für Sept 20").',
                },
                'month_covered': {'type': 'string', 'description': 'Haupt-Monat dieses Plans YYYY-MM (z.B. „2025-03").'},
            },
        },
    }

    prompt = f"""Du liest einen Lufthansa CAS / Dienstplan / Roster für Steuerjahr {year}.

═══ FORMAT ═══
CAS ist eine tabellarische Aufstellung pro Tag mit:
  • Wochentag-Kürzel + Tag-Nummer (z.B. „Mo 10", „Di 18", „Mi 06")
  • Aktivitäts-Code:
      - „LH<Nummer>" oder „FL" → flight (Linienflug)
      - „EH", „EMCRM", „SECCRM", „TK", „D4", „EM" → training (Schulung)
      - „SIM" → simulator
      - „ORTSTAG", „FRS", „LMN_AS", „LMN_CR" → office (Bürodienst Homebase)
      - „RES_SB", „RES", „SBY" → standby
      - „U", „U1", „U2", „URLAUB" → vacation
      - „K", „KRANK" → sick
      - „OFF", „X", „FREI" → free
      - alles andere → unknown
  • Zeiten:
      - Briefingzeit: oft „Briefingzeit(LT FRA): DD/MM/YY HH:MM"
      - Flugzeit: „LH600 A340 FRA 12:20-17:15 IKA"
      - Schulung: „EH 4 FRA 08:00-12:45"
      - Standby-Fenster: „RES_SB FRA 04:00-20:00"
  • Location: meist FRA Briefing oder remote (IKA, BLR, ORD etc.)

═══ WAS DU LIEFERN MUSST ═══
Pro Kalendertag im Plan ein Eintrag (auch frei/OFF/Urlaubs-Tage).
Wenn der Plan einen Monat abdeckt: 28-31 Tage. Wenn 2 Monate: alle Tage beider Monate.

Felder pro Tag:
  - date: YYYY-MM-DD (das Jahr ist {year}, der Monat aus dem Plan-Header)
  - activity_type: aus enum oben
  - marker: Roh-Code wie er im CAS steht
  - start_time: HH:MM (Briefing oder Aktivitäts-Start). Bei frei/Urlaub: leer.
  - end_time: HH:MM (letzte Flug-Landung oder Aktivitäts-Ende). Bei Tour über Mitternacht: bis 23:59 dieses Tags.
  - duration_minutes: end_time - start_time in Minuten
  - location: Briefing-Location IATA (meist FRA)
  - flights[]: nur bei activity_type='flight'
  - overnight_after_day: True wenn Tag endet in remote Location (Layover)
  - layover_ort: IATA-Code falls remote, leer wenn FRA
  - confidence: high/medium/low
  - raw_excerpt: kurzer Roh-Auszug (max 80 Zeichen)

═══ WICHTIG ═══
✓ KEINE STEUERLICHE BEWERTUNG. Liefere nur Lese-Fakten.
✓ KEINE Berechnung von >8h / Z72 / Z73. Backend rechnet.
✓ Wenn ein Tag unklar ist: activity_type='unknown' + confidence='low'.
✓ Bei Konflikten oder Lücken: in warnings[] notieren.
✓ Tage immer als YYYY-MM-DD im Steuerjahr {year}.

═══ KOMPAKTHEIT ═══
  • raw_excerpt max 80 Zeichen
  • flights[] nur ausgefüllt bei flight-Tagen
  • Output sollte in 25k Tokens passen

LIEFERE jetzt via Tool 'submit_cas_days' die strukturierten Tagesdaten."""

    content = [
        {'type': 'document', 'source': {'type': 'base64', 'media_type': 'application/pdf',
                                          'data': _b64.b64encode(pdf_bytes).decode()}},
        {'type': 'text', 'text': prompt},
    ]

    import time as _t
    start = _t.time()
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
        resp = client.messages.create(
            model='claude-sonnet-4-6',
            max_tokens=25000,
            tools=[cas_tool],
            tool_choice={'type': 'tool', 'name': 'submit_cas_days'},
            messages=[{'role': 'user', 'content': content}],
        )
    except Exception as e:
        print(f"[Sonnet-CAS] fail: {type(e).__name__}: {str(e)[:200]}")
        return None

    elapsed = _t.time() - start
    stop_reason = getattr(resp, 'stop_reason', None) if resp else 'no_response'
    usage = getattr(resp, 'usage', None) if resp else None
    if usage:
        in_tok = getattr(usage, 'input_tokens', '?')
        out_tok = getattr(usage, 'output_tokens', '?')
        print(f"[Sonnet-CAS] {source_filename[:40]}: stop={stop_reason} in_tok={in_tok} out_tok={out_tok} {elapsed:.1f}s")

    # Tool-Output extrahieren
    tool_input = None
    for block in (resp.content if resp else []):
        btype = getattr(block, 'type', None) if not isinstance(block, dict) else block.get('type')
        bname = getattr(block, 'name', None) if not isinstance(block, dict) else block.get('name')
        if btype == 'tool_use' and (bname == 'submit_cas_days' or bname is None):
            tool_input = block.get('input') if isinstance(block, dict) else getattr(block, 'input', None)
            if tool_input: break
    if not tool_input:
        print(f"[Sonnet-CAS] kein tool_input für {source_filename[:40]} (stop={stop_reason})")
        return None

    days_raw = tool_input.get('days', []) if isinstance(tool_input, dict) else []
    warnings_raw = tool_input.get('warnings', []) if isinstance(tool_input, dict) else []
    month_covered = tool_input.get('month_covered', '') if isinstance(tool_input, dict) else ''

    # Per-Day-Validation + Sanity
    days_normalized = []
    sanity_warnings = []
    for d in days_raw:
        ok, normalized, warn = _validate_cas_day(d)
        if ok and normalized:
            days_normalized.append(normalized)
        if warn:
            sanity_warnings.append(warn)

    warnings_combined = [str(w) for w in warnings_raw] + sanity_warnings

    print(f"[Sonnet-CAS] {source_filename[:40]}: {len(days_normalized)} Tage normalisiert "
          f"(Monat={month_covered}, {len(warnings_combined)} Warnungen)")

    return {
        'days': days_normalized,
        'warnings': warnings_combined,
        'month_covered': month_covered,
        'source_filename': source_filename,
    }


def _sonnet_read_cas_structured(cas_bytes, year=2025, homebase='FRA', job_id=None,
                                 source_filenames=None):
    """v11 CAS-Main-Reader: liest 1-N CAS-PDFs strukturiert pro Tag.

    Architektur:
      - Eine Sonnet-Anfrage pro PDF (memory-bounded)
      - Cache via SHA-256 file_hash + _CAS_PARSER_VERSION (reuse v10.4.1 chunk-cache)
      - Multi-File-Merge: dedup gleiche Datei, conflict-detection bei
        unterschiedlichen Daten für selben Tag aus zwei Files
      - Heartbeat pro Datei (_heartbeat_phase)

    Returns:
      {
        'days': [{date, activity_type, ..., source_file_id, ...}],  # alle Tage gemerged
        'conflicts': [{date, sources: [...]}],
        'warnings': [...],
        '_files_total': int,
        '_files_processed': int,
        '_cache_hits': int,
      }
      oder None wenn kein PDF lesbar war.
    """
    if not cas_bytes:
        return None
    cas_list = _bytes_list(cas_bytes) if cas_bytes else []
    if not cas_list:
        return None
    if source_filenames is None:
        source_filenames = [f'cas_{i+1}.pdf' for i in range(len(cas_list))]

    import hashlib as _hl

    all_days_by_date = {}  # date → list of {day_dict, source_file_id, source_filename}
    all_warnings = []
    cache_hits = 0
    files_processed = 0

    for idx, pdf_bytes in enumerate(cas_list):
        if not pdf_bytes:
            continue
        fname = source_filenames[idx] if idx < len(source_filenames) else f'cas_{idx+1}.pdf'
        file_hash = _hl.sha256(pdf_bytes).hexdigest()[:32]
        _heartbeat_phase(job_id, f'cas_file_{idx+1}_of_{len(cas_list)}',
                         {'file': fname[:40], 'idx': idx,
                          'label': f'Dienstplan/CAS wird gelesen (Datei {idx+1} von {len(cas_list)})…'})

        # Cache-Lookup via file_hash + parser_version
        cached = find_cached_chunk(file_hash, 'cas', 0, _CAS_PARSER_VERSION) if file_hash else None
        if cached and isinstance(cached, dict) and cached.get('days'):
            cas_days = cached.get('days') or []
            cas_warnings = cached.get('warnings') or []
            cache_hits += 1
            print(f"[CAS-Reader] {fname[:40]} cache HIT — {len(cas_days)} Tage (kein Sonnet-Call)")
        else:
            # Sonnet-Call für diese PDF
            result = _sonnet_read_cas_single_pdf(pdf_bytes, year, homebase, source_filename=fname)
            if not result:
                all_warnings.append(f'CAS-Datei {fname[:40]}: konnte nicht gelesen werden')
                continue
            cas_days = result.get('days') or []
            cas_warnings = result.get('warnings') or []
            # Cache speichern für Re-Runs
            if job_id and file_hash:
                chunk_id = create_job_chunk(job_id, 'cas', idx,
                                             page_from=None, page_to=None,
                                             file_hash=file_hash,
                                             parser_version=_CAS_PARSER_VERSION)
                if chunk_id:
                    save_job_chunk_result(chunk_id, {
                        'days': cas_days,
                        'warnings': cas_warnings,
                        'source_filename': fname,
                    })

        files_processed += 1
        all_warnings.extend([f'{fname[:30]}: {w}' for w in cas_warnings])

        # Merge: alle Tage indexieren mit source
        for day in cas_days:
            date = day.get('date')
            if not date: continue
            day_copy = dict(day)
            day_copy['source_file_id'] = file_hash
            day_copy['source_filename'] = fname
            all_days_by_date.setdefault(date, []).append(day_copy)

        # Memory-Release pro Datei (chunked design)
        gc.collect()

    # Hybrid-Check: Konflikt-Detection bei Duplikaten
    merged_days = []
    conflicts = []
    for date in sorted(all_days_by_date):
        candidates = all_days_by_date[date]
        if len(candidates) == 1:
            merged_days.append(candidates[0])
            continue
        # Mehrere Quellen für selben Tag → check ob inhaltlich identisch
        sigs = set((c.get('activity_type'), c.get('start_time'), c.get('end_time'), c.get('marker'))
                   for c in candidates)
        if len(sigs) == 1:
            # Identisch — dedupe, behalte ersten
            merged_days.append(candidates[0])
        else:
            # Konflikt — neueste Datei gewinnt (vorletzte in der Liste = neuerer NTF)
            # Heuristik: letzte ist die zuletzt geparste = oft die neuere
            chosen = candidates[-1]
            merged_days.append(chosen)
            conflicts.append({
                'date': date,
                'reason': 'multiple_files_disagree',
                'candidates': [{'activity_type': c.get('activity_type'),
                                'start_time': c.get('start_time'),
                                'end_time': c.get('end_time'),
                                'marker': c.get('marker'),
                                'source': c.get('source_filename')} for c in candidates],
                'chosen_source': chosen.get('source_filename'),
            })

    print(f"[CAS-Reader] FERTIG: {files_processed}/{len(cas_list)} Files, "
          f"{len(merged_days)} Tage, {len(conflicts)} Konflikte, {cache_hits} cache hits")
    _heartbeat_phase(job_id, 'cas_merge_complete',
                     {'days': len(merged_days), 'conflicts': len(conflicts), 'cache_hits': cache_hits,
                      'label': 'Dienstplan zusammengeführt…'})

    if not merged_days:
        return None

    return {
        'days': merged_days,
        'conflicts': conflicts,
        'warnings': all_warnings,
        '_files_total': len(cas_list),
        '_files_processed': files_processed,
        '_cache_hits': cache_hits,
        '_parser_version': _CAS_PARSER_VERSION,
    }


def _sonnet_read_dp_structured(dp_bytes, einsatz_bytes=None, year=2025, homebase='FRA',
                                page_range_hint=None, max_tokens_override=None):
    """v7.0 Pre-Reader: Sonnet liest Flugstundenübersichten strukturiert pro Tag aus.
    Liefert NUR Lese-Fakten — keine steuerliche Klassifikation!

    Kein Einsatzplan mehr. Der einsatz_bytes-Parameter bleibt aus Kompatibilität,
    wird aber ignoriert.

    Output-Format:
    {
      "days": [
        {
          "datum": "2025-03-06",
          "raw_marker": "LH... A FRA-DUB",
          "markers": ["A"],
          "routing": ["FRA", "DUB"],
          "activity_type": "tour_start",   # frei/urlaub/krank/standby/office/tour_start/tour_continuation/tour_end/same_day/none
          "has_flight": true,
          "has_fl": true,                  # FL-Marker im DP
          "layover_ort": "DUB",
          "layover_inland": false,
          "homebase_heimkehr": false,      # endet User heute zuhause?
          "overnight_after_day": true,     # schläft User auswärts nach diesem Tag?
          "tour_id": "2025-03-06_DUB_1",
          "tour_open": true,
          "confidence": 0.86,
          "notes": "..."
        },
        ...
      ],
      "warnings": [...]
    }
    """
    if not ANTHROPIC_KEY:
        return None
    dp_bytes = _bytes_list(dp_bytes) if dp_bytes else []
    # einsatz_bytes-Parameter wird ignoriert (v7: nur 3 Pflichtdokumente)
    if not dp_bytes:
        return None

    structured_tool = {
        'name': 'submit_structured_days',
        'description': 'Liefere strukturierte Tagesdaten als reine Lese-Fakten. KEINE steuerliche Klassifikation.',
        'input_schema': {
            'type': 'object',
            'required': ['days'],
            'properties': {
                'days': {
                    'type': 'array',
                    'description': f'Liste ALLER sichtbaren Tage in {year}. Tage NIEMALS still auslassen — wenn unklar: activity_type="unknown" + raw_lines + notes. Frei/Urlaub/Krank werden mit activity_type="frei"/"urlaub"/"krank" geliefert (NICHT weggelassen). Nur lange Frei-Sequenzen (z.B. mehrwöchiger Urlaub) können auf einen activity-Tag reduziert werden.',
                    'items': {
                        'type': 'object',
                        'required': ['datum', 'activity_type'],
                        'properties': {
                            'datum': {'type': 'string', 'description': 'YYYY-MM-DD'},
                            'activity_type': {
                                'type': 'string',
                                'enum': ['frei', 'urlaub', 'krank', 'standby', 'office', 'training', 'tour', 'same_day', 'unknown'],
                                'description': 'tour=mehrtägige Tour-Tag (Anreise/Mittel/Heimkehr), same_day=Tagestrip, training=Schulung, office=Bürotag Homebase, standby=SBY/RES zuhause'
                            },
                            'routing': {'type': 'array', 'items': {'type': 'string'}, 'description': 'IATA-Codes z.B. ["FRA","BLR"]'},
                            'has_fl': {'type': 'boolean', 'description': 'FL-Marker im Dienstplan'},
                            'overnight_after_day': {'type': 'boolean', 'description': 'KRITISCH: User schläft NACH diesem Tag auswärts (Hotel)? True bei FL/Tour-Layover. False bei Same-Day/FREI/Standby/Heimkehr.'},
                            'layover_ort': {'type': 'string', 'description': 'IATA wo User heute Nacht schläft, leer wenn zuhause'},
                            'layover_inland': {'type': 'boolean', 'description': 'true=Inland (FRA/MUC/HAM/DUS/STR/CGN/HAJ/BER/LEJ/NUE/BRE), false=Ausland, weglassen wenn keine Übernachtung'},
                            # ── v8.1: Commute-/Workday-/Dauer-Felder ──
                            'starts_at_homebase': {'type': 'boolean', 'description': 'true wenn Dienst/Routing am Homebase BEGINNT (Tour-Anreise, Same-Day-Start, Office, Training-Anreise). false bei Tourfortsetzung/Layover-Tag.'},
                            'ends_at_homebase': {'type': 'boolean', 'description': 'true wenn Dienst/Routing am Homebase ENDET (Heimkehr, Same-Day-Ende, Office). false bei Layover-Tag/auswärtiger Übernachtung.'},
                            'is_workday': {'type': 'boolean', 'description': 'true bei tour/same_day/office/training/standby. false bei frei/urlaub/krank/LM-Nachgewährung/unknown ohne Dienstkontext.'},
                            'requires_commute': {'type': 'boolean', 'description': 'true NUR wenn der User HEUTE neu von zuhause zur Homebase fährt: Tourstart ab Homebase, Same-Day ab Homebase, Office/Training an Homebase. false bei Layover-Tag, Tourfortsetzung, Heimkehrtag ohne neue Anfahrt, Standby zuhause, frei/urlaub/krank.'},
                            'explicit_daily_commute': {'type': 'boolean', 'description': 'NUR bei mehrtägigem Training/Seminar setzen: true wenn aus DP klar erkennbar ist dass User TÄGLICH von zuhause zur Schulung fährt (z.B. tägliche Briefingzeiten + Heimkehr). false oder weglassen bei Block-Schulung mit nur 1 Anfahrt.'},
                            'start_time': {'type': 'string', 'description': 'HH:MM Dienstbeginn (z.B. "06:30"), leer wenn nicht im DP'},
                            'end_time': {'type': 'string', 'description': 'HH:MM Dienstende (z.B. "18:45"), leer wenn nicht im DP'},
                            'duty_duration_minutes': {'type': 'integer', 'description': 'Dienstdauer in Minuten (start→end), nur setzen wenn beide Zeiten erkennbar sind. Bei Tour über Mitternacht: bis Mitternacht.'},
                            'time_is_absence': {'type': 'boolean', 'description': 'true wenn start_time/end_time die TOUR-ABWESENHEIT (Tür-zu-Tür inkl. Anfahrt) repräsentieren, false wenn nur Dienstbeginn/-ende. Sonnet/AeroTAX-Reader liefert idR. Dienstzeit (false). FollowMe-Style-Daten wären absence-time (true). Default false.'},
                            'raw_marker': {'type': 'string', 'description': 'Roher Marker-String wie im DP (max 30 Zeichen), z.B. "FL738 FRA-DUB"'},
                            'raw_lines': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Rohe Zeilen aus dem DP für diesen Tag (max 3 Zeilen, jeweils max 80 Zeichen) — nur bei Unsicherheit oder Same-Day befüllen.'},
                            'confidence': {'type': 'number', 'description': '0-1 wie sicher'},
                            'notes': {'type': 'string', 'description': 'Optionaler Kurzhinweis bei Unsicherheit (max 60 Zeichen)'},
                        }
                    }
                },
                'warnings': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': 'Lese-Warnungen z.B. unklares Routing, mehrdeutige Marker'
                }
            }
        }
    }

    # PDF-Cap: insgesamt max 24 Documents an Sonnet (Anthropic-Limit ist 100,
    # aber mehr als ~25 macht den Output zu groß für strukturierte Tagesdaten).
    content = []
    pdf_count = 0
    for pdf_bytes in dp_bytes[:24]:
        try:
            content.append({
                'type': 'document',
                'source': {'type': 'base64', 'media_type': 'application/pdf',
                           'data': base64.b64encode(pdf_bytes).decode()}
            })
            pdf_count += 1
        except: pass
    if pdf_count == 0:
        return None

    einsatz_hinweis = ""

    prompt = f"""Du bist ein DOKUMENTEN-PARSER für Lufthansa-Flugstundenübersichten (Steuerjahr {year}, Homebase {homebase}).

═════ ZWINGEND: TOOL VERWENDEN ══════════════════════════════════════════════

Du MUSST das Tool 'submit_structured_days' aufrufen. Antworte NICHT mit
Freitext, NICHT mit JSON im Text, NICHT mit Erklärungen außerhalb des Tools.
Auch wenn du unsicher bist: Gib trotzdem strukturierte Tagesdaten mit
confidence < 0.6 + warnings zurück. Lieber unsichere Daten als kein Tool-Aufruf.
{einsatz_hinweis}
═════ DEINE AUFGABE ═════════════════════════════════════════════════════════

Lies den Dienstplan strukturiert. Liefere für jeden Kalendertag MIT
dienstlicher Aktivität ein Dict mit datum + activity_type +
overnight_after_day. Andere Felder optional aber empfohlen wo bekannt.

DU LIEST NUR FAKTEN AUS — DU KLASSIFIZIERST NICHT STEUERLICH (Z72/Z73/Z76)!
Das macht ein anderer Schritt mit deinen Daten als Input.

═════ FELDER PRO TAG (KOMPAKT) ══════════════════════════════════════════════

PFLICHT: datum, activity_type
EMPFOHLEN: routing, has_fl, overnight_after_day, layover_ort, layover_inland

▸ activity_type — wähle einen Wert:
  • frei (F-Marker), urlaub (U), krank (K)
  • standby (SBY/RES, zuhause)
  • office (EM/EH/Office am Homebase mit täglicher Heimkehr)
  • training (Schulung — D4/DD/EM/EH mehrtägig oder einzeln; bei mehrtägiger
    Schulung mit auswärtigem Ort: overnight_after_day=true setzen)
  • tour (mehrtägige Tour-Tag — Anreise/Mittel/Heimkehr alle "tour")
  • same_day (Tagestrip, A+E am gleichen Tag, kein FL)
  • unknown (wenn Marker unklar)

▸ has_fl: TRUE wenn FL-Marker im Dienstplan. HARTER LESE-FAKT.

▸ overnight_after_day: KRITISCHSTES Feld!
  TRUE = User schläft NACH diesem Tag AUSWÄRTS
  FALSE = User schläft NACH diesem Tag ZUHAUSE
  • Same-Day → FALSE
  • Tour-Tag mit FL/Layover → TRUE
  • Tour-Heimkehr-Tag (letzter Tag) → FALSE
  • FREI/Standby/Office → FALSE
  • Mehrtägige Schulung auswärts (alle Tage außer letzter) → TRUE

▸ layover_ort: 3-Letter-IATA wo User HEUTE NACHTS schläft.
  Leer wenn zuhause. Bei overnight_after_day=true: setze layover_ort.

▸ layover_inland: TRUE wenn layover_ort einer dieser Codes ist:
  FRA, MUC, HAM, DUS, STR, CGN, HAJ, BER, TXL, SXF, LEJ, NUE, BRE,
  FMO, PAD, NRN, FKB, HHN, SCN, DRS, ERF, FDH, RLG, KSF.
  FALSE bei Auslandscodes. Weglassen wenn keine Übernachtung.

═════ COMMUTE-/WORKDAY-FELDER (v8) ═════════════════════════════════════════

▸ starts_at_homebase: TRUE wenn der DIENST HEUTE am Homebase ({homebase}) beginnt.
  • Tour-Anreise (FRA→BLR): true
  • Same-Day (FRA→TXL→FRA): true
  • Office/Training am Homebase: true
  • Layover-Tag (BLR→BLR oder BLR→TYO): false
  • Tourfortsetzung von Vortag-Layover: false
  • Heimkehrtag (BLR→FRA aus Layover): false (Dienst beginnt auswärts)

▸ ends_at_homebase: TRUE wenn der DIENST HEUTE am Homebase ({homebase}) endet.
  • Same-Day: true
  • Heimkehrtag (BLR→FRA): true
  • Office/Training ohne Übernachtung: true
  • Layover-Tag mit auswärtiger Übernachtung: false
  • Tour-Anreise ohne Heimkehr: false

▸ is_workday: TRUE bei echtem Dienst:
  tour, same_day, office, training, standby = TRUE
  frei, urlaub, krank, LM-Nachgewährung = FALSE
  unknown ohne Dienstkontext = FALSE

▸ requires_commute: TRUE wenn User HEUTE neu von zuhause zur Homebase fährt.
  Das ist eine STRENGERE Bedingung als is_workday — viele Workdays haben
  KEIN requires_commute (Layover-Tag, Tourfortsetzung, Standby zuhause).
  • Tour-Anreise ab Homebase: TRUE (Heimfahrt → Homebase)
  • Same-Day ab Homebase: TRUE
  • Office/Training an Homebase: TRUE
  • Layover-Tag (auswärts schlafen): FALSE
  • Tourfortsetzung mitten in Tour: FALSE
  • Heimkehrtag ohne neue Anfahrt: FALSE
  • Standby zuhause (kein Weg zur Homebase): FALSE
  • Frei/Urlaub/Krank: FALSE
  Faustregel: requires_commute = starts_at_homebase = "Tag beginnt mit
  Anfahrt von zuhause".

▸ start_time / end_time: Dienstbeginn/-ende im DP, falls erkennbar (HH:MM).
  Bei Mehrtagestour: pro Tag die heutige Briefing-/Off-Duty-Zeit.
  WICHTIG: Auch für Office/Schulung/Bürodienst-Marker explizit lesen!
    - EK BUERODIENST z.B. 08:38–18:52
    - D4 SCHULUNG    z.B. 08:08–17:22
    - EH/EM/SM/TK    z.B. 08:38–18:22
  Diese Zeiten sind essentiell für die Z72-Klassifikation (Inland >8h).
  NIEMALS leer lassen wenn der DP klare Zeiten zeigt.

▸ duty_duration_minutes: Minuten zwischen start_time und end_time. Nur setzen
  wenn beide Zeiten klar im DP stehen. Bei Tagen über Mitternacht: bis 23:59.
  Wichtig für Z72-Plausibilisierung: Dienst >480min (8h) ohne Übernachtung.
  Office/Schulung mit duty>=480min an Homebase = Inland-Tagestrip Z72 (14€).
  Office/Schulung mit duty<480min = kein Z72.

▸ time_is_absence: false (Default) wenn start/end nur Dienst-Slot sind
  (Briefing→Off-Duty). Backend addiert dann +2×commute für Z72-Plausi.
  true wenn start/end die TOUR-ABWESENHEIT sind (Tür-zu-Tür inkl. Anfahrt) —
  Backend nutzt duty_duration_minutes direkt, KEIN doppelter commute.
  AeroTAX-DP-Reader liefert idR Dienst-Zeit → false. Externer Import
  (FollowMe-Style) liefert Abwesenheits-Zeit → true.

▸ raw_marker: Originaler Marker-String aus dem DP (max 30 Zeichen).
  Beispiele: "FL738 FRA-DUB", "EH FRA", "SBY", "F".

▸ raw_lines: 1-3 rohe Zeilen aus dem DP für diesen Tag (max 80 Zeichen je
  Zeile). NUR befüllen bei: Same-Day-Tag, unklarer Klassifikation,
  ungewöhnlichem Marker. Nicht für offensichtliche Tour-/FL-/Frei-Tage.

═════ CREW-MARKER-LEXIKON (LH-Dienstpläne) ════════════════════════════════════

Erkenne diese Crew-typischen Marker. Bewerte KEINE EASA-/FTL-Legalität —
nutze die Begriffe nur als Lesehilfe für Tagesart, Routing, Overnight und
Homebase-Kontext.

▸ Flug-/Tour-Dienste:
  • LH#### (LH-Flugnummer) ODER mehrere LH-Segmente an einem Tag
  • Routing mit IATA-Codes, z. B. "{homebase}-BLR", "BLR-{homebase}",
    "{homebase}-CPH-{homebase}"
  → activity_type=tour bei Layover/Overnight, =same_day bei A+E selber Tag

▸ FL = Layover-/Freizeit-/Hotel-Tag innerhalb einer Tour
  • FL ist KEIN neuer Tourstart, sondern Tour-Mittel-Tag
  • FL kann Hotelnacht anzeigen → overnight_after_day=true
  • FL nach Heimkehr: kein neuer Tourstart, kein automatisches Hotel
  • activity_type bleibt "tour", auch bei FL-Marker

▸ Standby / Bereitschaft:
  • SB, RB, RE, "Bereitschaft", "Rufbereitschaft"
  → activity_type=standby
  → is_workday=true, requires_commute=false (zuhause), kein VMA, kein Hotel

▸ Office / Schulung an Homebase:
  • EM, EH, TK, D4, "Erste Hilfe", "Emergency Training", "Office"
  → activity_type=office (täglich Heimkehr) ODER training (mehrtägig)
  → is_workday=true, requires_commute=true (Anfahrt zur Homebase)

▸ Frei / Urlaub / Krank / nicht-dienstlich:
  • frei, OF, FR, /-, U, U1, "Urlaub", K, "krank"
  → activity_type=frei/urlaub/krank
  → is_workday=false, requires_commute=false, kein VMA, keine Hotelnacht

▸ LM Nachgewährung:
  • "LM NACHGEWAEHRUNG", "LM Nachgewähr"
  → activity_type=frei
  → is_workday=false, KEIN Arbeitstag, kein Fahrtag, kein VMA

▸ Proceeding / Positioning / Deadhead / DH:
  • Dienstlich relevant. Kann Same-Day/Z72 sein, wenn >8h und keine Übernachtung
  • Kann Teil eines Tourclusters sein
  • activity_type=tour oder same_day je nach Overnight
  • NICHT automatisch Hotel/Fahrtag

▸ Unknown:
  • Nur verwenden wenn Marker WIRKLICH unklar ist
  • activity_type=unknown wird NICHT als Arbeitstag gezählt
  • Bei aktiver SE-Zeile + Cluster-Kontext kann Backend reklassifizieren

═════ MULTI-STOP-TOUREN — JEDEN TAG EINZELN ═══════════════════════════════════

Bei FRA→BER→ZAG→ARN→FRA Multi-Stop-Tour: jeder Kalendertag bekommt seine
eigenen Felder. Beispiel:
  Tag 1: layover_ort="BER", layover_inland=true,  overnight_after_day=true
  Tag 2: layover_ort="ZAG", layover_inland=false, overnight_after_day=true
  Tag 3: layover_ort="ARN", layover_inland=false, overnight_after_day=true
  Tag 4: homebase_heimkehr=true, overnight_after_day=false

═════ ANTI-MUSTER (NICHT MACHEN) ═════════════════════════════════════════════

❌ NICHT die ganze Tour mit dem layover_ort des LETZTEN Layovers füllen
❌ NICHT layover_inland=false setzen wenn der konkrete Tag Inland-Layover hat
❌ NICHT overnight_after_day=false bei FL-Marker (FL bedeutet Übernachtung!)
❌ NICHT steuerlich klassifizieren (kein "Z72/Z73/Z76" in den notes — das
   macht ein anderer Schritt)

═════ VOLLSTÄNDIGKEIT — WICHTIGER ALS KOMPAKTHEIT (v8.18) ═══════════════════

KRITISCH: Liefere JEDEN sichtbaren Tag aus dem Dienstplan-PDF als eigenes
day-object. Niemals einen sichtbaren Tag still auslassen.

Wenn der Marker für einen Tag UNKLAR ist:
  • activity_type="unknown"
  • confidence < 0.5
  • raw_lines: 1-2 rohe Zeilen aus dem PDF
  • notes: kurze Erklärung warum unklar

Frei/Urlaub/Krank-Tage:
  • Liefere als activity_type="frei"/"urlaub"/"krank"
  • NICHT weglassen — der Backend-Klassifikator braucht die Info dass
    "an diesem Tag war FREI markiert" (nicht "kein Tag im PDF")
  • Ausnahme: Mehrwöchiger Urlaub-Block kann auf 1-2 Tage reduziert werden
    (z.B. "01.07-31.07 Urlaub" → 1 Eintrag)

Tour-Mittel-Tage:
  • Liefere alle Layover-/FL-Tage einer Tour als eigene day-objects
  • Keine Auslassung weil "war ja eh nur Layover"

Kompaktheit-Regeln:
  • raw_marker max 30 Zeichen
  • notes max 60 Zeichen
  • raw_lines nur bei Unsicherheit oder Same-Day

Ziel: Output passt in 60k Tokens. Bei 365 Tagen mit kurzen Einträgen
machbar. Vollständigkeit > Kompaktheit.

WICHTIG: Auch wenn das Dokument nur einzelne Monate enthält oder
unvollständig ist — extrahiere jeden erkennbaren Tag. Liefere lieber
20 Tage als 0. Wenn das Dokument nicht lesbar ist, schreibe das in
warnings und gib so viele Tage zurück wie möglich.

LIEFERE jetzt via Tool die strukturierten Tagesdaten."""

    # v10.4: page_range_hint — chunked DP-Pipeline gibt Sonnet einen Seitenbereich
    # vor. Spart Output-Tokens (jeder Chunk ist klein) → kleinerer Memory-Peak.
    if page_range_hint and isinstance(page_range_hint, (tuple, list)) and len(page_range_hint) == 2:
        _pf, _pt = page_range_hint
        prompt += (
            f"\n\n═════ CHUNK-FOKUS ════════════════════════════════════════════════════════════\n"
            f"WICHTIG: Lies AUSSCHLIESSLICH die Seiten {_pf} bis {_pt} dieses Dokuments.\n"
            f"Ignoriere alle anderen Seiten. Liefere NUR Tage die auf den Seiten {_pf}-{_pt}\n"
            f"sichtbar sind. Wenn ein Tag teils auf einer früheren/späteren Seite war,\n"
            f"liefere ihn trotzdem wenn du das Datum + die Aktivität klar erkennst.\n"
        )

    content.append({'type': 'text', 'text': prompt})

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=300.0)
    import time as _t
    start = _t.time()
    try:
        resp = None
        for attempt in range(2):
            try:
                # v10.2: max_tokens 32k → 60k. 32k war zu eng für volle 12 Monate.
                # v10.4: max_tokens_override — chunked DP nutzt 20k pro Chunk
                # (kleiner Memory-Peak, gc.collect zwischen Chunks).
                _dp_max_tok = int(max_tokens_override) if max_tokens_override else 60000
                resp = client.messages.create(
                    model='claude-sonnet-4-6', max_tokens=_dp_max_tok,
                    tools=[structured_tool],
                    tool_choice={'type': 'tool', 'name': 'submit_structured_days'},
                    messages=[{'role': 'user', 'content': content}]
                )
                break
            except Exception as e:
                if attempt == 1: raise
                print(f"[Sonnet-DP-Structured] retry: {str(e)[:100]}")
                _t.sleep(5)
        elapsed = _t.time() - start
        # Detailliertes Logging zum Debuggen — content blocks + stop_reason
        stop_reason = getattr(resp, 'stop_reason', None) if resp else 'no_response'
        usage = getattr(resp, 'usage', None) if resp else None
        if usage:
            in_tok = getattr(usage, 'input_tokens', '?')
            out_tok = getattr(usage, 'output_tokens', '?')
            print(f"[Sonnet-DP-Structured] resp stop={stop_reason} in_tok={in_tok} out_tok={out_tok}")
        # Robustes Tool-Parsing: object UND dict, plus expliziter Tool-Name-Check
        tool_input = None
        block_summary = []
        for block in (resp.content if resp else []):
            btype = getattr(block, 'type', None) if not isinstance(block, dict) else block.get('type')
            bname = getattr(block, 'name', None) if not isinstance(block, dict) else block.get('name')
            block_summary.append(f"{btype}:{bname or '_'}")
            if btype == 'tool_use' and (bname == 'submit_structured_days' or bname is None):
                # Input kann object oder dict sein
                if isinstance(block, dict):
                    tool_input = block.get('input')
                else:
                    tool_input = getattr(block, 'input', None)
                if tool_input:
                    break
        if not tool_input:
            # Falls Sonnet als Text geantwortet hat: ersten 500 Zeichen loggen
            text_snippet = ''
            for block in (resp.content if resp else []):
                if (getattr(block, 'type', None) == 'text') or (isinstance(block, dict) and block.get('type') == 'text'):
                    txt = getattr(block, 'text', None) or (block.get('text', '') if isinstance(block, dict) else '')
                    text_snippet = (txt or '')[:500]
                    break
            print(f"[Sonnet-DP-Structured] kein tool_input — stop={stop_reason} blocks={block_summary}")
            if text_snippet:
                print(f"[Sonnet-DP-Structured] text-fallback Sonnet sagte: {text_snippet[:300]}")
            return None
        # tool_input kann auch dict sein wenn SDK serialisiert
        days_raw = tool_input.get('days', []) if isinstance(tool_input, dict) else []
        warnings_raw = tool_input.get('warnings', []) if isinstance(tool_input, dict) else []
        # Diagnostic-Log vor Normalisierung
        if isinstance(days_raw, list) and days_raw:
            print(f"[Sonnet-DP-Structured] raw days type=list[{type(days_raw[0]).__name__}] len={len(days_raw)}")
        elif isinstance(days_raw, dict):
            print(f"[Sonnet-DP-Structured] raw days is DICT not list — keys[:5]={list(days_raw.keys())[:5]}")
        # Robuste Normalisierung — auch dict-input/tuple-items werden zu list[dict]
        days = _normalize_v6_classifications(days_raw)
        days = [d for d in days if d.get('datum')]
        warnings = [str(w) if not isinstance(w, str) else w for w in (warnings_raw or [])]
        # Normalisiere: stelle sicher dass alle Felder existieren
        for d in days:
            d.setdefault('markers', [])
            d.setdefault('routing', [])
            d.setdefault('has_flight', False)
            d.setdefault('has_fl', False)
            d.setdefault('layover_ort', '')
            d.setdefault('homebase_heimkehr', False)
            d.setdefault('overnight_after_day', False)
            d.setdefault('tour_id', '')
            d.setdefault('tour_open', False)
            d.setdefault('confidence', 1.0)
            d.setdefault('notes', '')
            d.setdefault('raw_marker', '')
            # layover_inland: wenn nicht gesetzt, aus layover_ort ableiten
            if 'layover_inland' not in d or d['layover_inland'] is None:
                lo = d.get('layover_ort', '')
                d['layover_inland'] = _is_inland_code(lo) if lo else None
        print(f"[Sonnet-DP-Structured] {elapsed:.1f}s: {len(days)} Tage strukturiert "
              f"({sum(1 for d in days if d.get('overnight_after_day'))} mit Übernachtung)")
        # v10.3: Warnungen verbatim loggen, nicht nur Count. Bei 0 Tagen zusätzlich
        # raw_input-Snippet ausgeben, damit man sehen kann was Sonnet gesehen hat.
        if warnings:
            for _wi, _w in enumerate(warnings[:5]):
                print(f"[Sonnet-DP-Structured] warn[{_wi}]: {str(_w)[:300]}")
        if not days:
            try:
                _ti_snip = json.dumps(tool_input, ensure_ascii=False)[:600] if isinstance(tool_input, dict) else str(tool_input)[:600]
                print(f"[Sonnet-DP-Structured] 0-Tage-Diagnostic tool_input={_ti_snip}")
            except Exception:
                pass
        return {'days': days, 'warnings': warnings}
    except Exception as e:
        print(f"[Sonnet-DP-Structured] fail: {type(e).__name__}: {str(e)[:200]}")
        return None


def _count_dp_pdf_pages(dp_bytes_list):
    """v10.4: Zählt Total-Seiten über alle DP-PDFs via pdfplumber."""
    import pdfplumber as _pp, io as _io
    total = 0
    for pdf_bytes in (_bytes_list(dp_bytes_list) or []):
        if not pdf_bytes: continue
        try:
            with _pp.open(_io.BytesIO(pdf_bytes)) as pdf:
                total += len(pdf.pages)
        except Exception as e:
            print(f"[DP-Pages] count fail: {str(e)[:100]}")
    return total


def _dp_chunk_boundaries(total_pages, chunk_size_target=3):
    """v10.4: Berechnet Chunk-Grenzen für DP-Pipeline.
    Returns list of (page_from, page_to) tuples (1-indexed, inclusive).

    Strategie:
      - ≤4 Seiten: kein Chunking nötig → 1 Chunk
      - 5-12 Seiten: chunks à ~3 Seiten
      - >12 Seiten: chunks à ~4 Seiten
    """
    if total_pages <= 0:
        return [(1, 1)]
    if total_pages <= 4:
        return [(1, total_pages)]
    size = chunk_size_target if total_pages <= 12 else 4
    boundaries = []
    start = 1
    while start <= total_pages:
        end = min(start + size - 1, total_pages)
        boundaries.append((start, end))
        start = end + 1
    return boundaries


def _heartbeat_phase(job_id, phase_name, extra=None):
    """v10.4: Worker-Heartbeat — setzt job.phase + phase_updated_at.
    Stale-Job-Detector im Cleanup-Loop nutzt das um hängende Worker zu erkennen."""
    if not job_id:
        return
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id]['phase'] = phase_name
            _jobs[job_id]['phase_updated_at'] = datetime.utcnow().isoformat() + 'Z'
            if extra and isinstance(extra, dict):
                _jobs[job_id].update({f'phase_{k}': v for k, v in extra.items()})


def _sonnet_read_dp_structured_chunked_v104(dp_bytes, einsatz_bytes=None, year=2025,
                                             homebase='FRA', job_id=None):
    """v10.4: Chunked DP-Reader für Memory-Pressure-Reduktion auf Render Free-Tier.

    Strategie:
      1. PDF-Seiten zählen via pdfplumber
      2. Chunk-Grenzen berechnen (3-4 Seiten pro Chunk)
      3. Pro Chunk: _sonnet_read_dp_structured mit page_range_hint + max_tokens=20000
      4. Chunk-Result sofort in job_chunks persistieren
      5. Lokale Objekte freigeben + gc.collect() zwischen Chunks
      6. Am Ende: alle Chunks mergen, Tage deduplizieren

    Wenn PDF klein genug (≤4 Seiten): single call ohne Chunking (kein Overhead).

    Returns: dict {days, warnings} — gleiche Shape wie _sonnet_read_dp_structured.
    """
    if not dp_bytes:
        return None
    total_pages = _count_dp_pdf_pages(dp_bytes)
    boundaries = _dp_chunk_boundaries(total_pages)

    if len(boundaries) <= 1:
        # Klein genug für single-call (kein Chunking-Overhead)
        _heartbeat_phase(job_id, 'dp_single_call',
                         {'pages': total_pages,
                          'label': 'Flugstundenübersicht wird gelesen…'})
        return _sonnet_read_dp_structured(dp_bytes, einsatz_bytes, year, homebase)

    # v10.4.1: File-Hash für Cache-Lookup berechnen
    import hashlib as _hl
    dp_bytes_concat = b''.join((b if isinstance(b, bytes) else (b[0] if isinstance(b, tuple) else b''))
                                for b in (_bytes_list(dp_bytes) or []))
    file_hash = _hl.sha256(dp_bytes_concat).hexdigest()[:32] if dp_bytes_concat else None
    parser_version = _DP_PARSER_VERSION
    if file_hash:
        print(f"[DP-Chunked-v10.4.1] file_hash={file_hash[:12]} parser={parser_version}")

    print(f"[DP-Chunked-v10.4] {total_pages} Seiten → {len(boundaries)} Chunks (max_tokens=20000/Chunk)")
    all_days = []
    all_warnings = []
    failed_chunks = 0
    cache_hits = 0

    for idx, (pf, pt) in enumerate(boundaries):
        chunk_label = f"dp_chunk_{idx+1}_of_{len(boundaries)}"
        _heartbeat_phase(job_id, chunk_label,
                         {'page_from': pf, 'page_to': pt,
                          'chunk_index': idx, 'total_chunks': len(boundaries),
                          'label': f'Flugstundenübersicht wird ausgewertet (Abschnitt {idx+1} von {len(boundaries)})…'})

        # v10.4.1: Cache-Lookup zuerst — wenn diese Datei + dieser Chunk + diese
        # parser_version bereits ausgewertet wurde, kompletter Sonnet-Call gespart.
        cached = find_cached_chunk(file_hash, 'dp', idx, parser_version) if file_hash else None
        if cached and isinstance(cached, dict) and cached.get('days'):
            cache_days = cached.get('days') or []
            cache_warnings = cached.get('warnings') or []
            all_days.extend(cache_days)
            all_warnings.extend(cache_warnings)
            cache_hits += 1
            print(f"[DP-Chunked-v10.4.1] chunk {idx+1}/{len(boundaries)} cache HIT — {len(cache_days)} Tage (kein Sonnet-Call)")
            # Job-Chunk für dieses job_id anlegen + sofort als completed markieren
            # (zur Audit-Trace; cache_result ist die selbe Daten)
            if job_id:
                chunk_id = create_job_chunk(job_id, 'dp', idx, pf, pt,
                                             file_hash=file_hash,
                                             parser_version=parser_version)
                if chunk_id:
                    save_job_chunk_result(chunk_id, {
                        'days': cache_days, 'warnings': cache_warnings,
                        'days_count': len(cache_days),
                        'page_from': pf, 'page_to': pt,
                        '_from_cache': True,
                    })
            gc.collect()
            continue

        # 1. Chunk-Row in Supabase anlegen
        chunk_id = create_job_chunk(job_id, 'dp', idx, pf, pt,
                                     file_hash=file_hash,
                                     parser_version=parser_version) if job_id else None
        if chunk_id:
            mark_job_chunk_running(chunk_id)

        # 2. Sonnet-Call mit kleinerem max_tokens + page-range hint
        chunk_result = None
        try:
            chunk_result = _sonnet_read_dp_structured(
                dp_bytes, einsatz_bytes, year, homebase,
                page_range_hint=(pf, pt),
                max_tokens_override=20000,
            )
        except Exception as e:
            print(f"[DP-Chunked-v10.4] chunk {idx+1} crash: {type(e).__name__}: {str(e)[:200]}")
            if chunk_id:
                mark_job_chunk_failed(chunk_id, 'sonnet_exception', f'{type(e).__name__}: {str(e)[:200]}')
            failed_chunks += 1
            # MEMORY: lokale Refs freigeben
            chunk_result = None
            gc.collect()
            continue

        if not chunk_result or not isinstance(chunk_result, dict):
            if chunk_id:
                mark_job_chunk_failed(chunk_id, 'no_result', 'Sonnet returned None')
            failed_chunks += 1
            chunk_result = None
            gc.collect()
            continue

        chunk_days = chunk_result.get('days') or []
        chunk_warnings = chunk_result.get('warnings') or []

        if not chunk_days:
            if chunk_id:
                mark_job_chunk_failed(chunk_id, 'zero_days', f'Sonnet returned 0 days for pages {pf}-{pt}')
            failed_chunks += 1
        else:
            # 3. Result persistieren (sanitized, keine PDF-bytes)
            if chunk_id:
                save_job_chunk_result(chunk_id, {
                    'days': chunk_days,
                    'warnings': chunk_warnings,
                    'days_count': len(chunk_days),
                    'page_from': pf, 'page_to': pt,
                })
            all_days.extend(chunk_days)
            all_warnings.extend(chunk_warnings)
            print(f"[DP-Chunked-v10.4] chunk {idx+1}/{len(boundaries)} (Seiten {pf}-{pt}): {len(chunk_days)} Tage")

        # 4. MEMORY-RELEASE — chunk-spezifische Refs freigeben + gc
        chunk_result = None
        chunk_days = None
        chunk_warnings = None
        gc.collect()
        try:
            _release_memory_to_os()
        except Exception:
            pass

    # 5. Merge + Dedupe — gleiches Datum nur einmal
    days_by_date = {}
    for d in all_days:
        if not isinstance(d, dict): continue
        dt = d.get('datum')
        if not dt: continue
        if dt not in days_by_date:
            days_by_date[dt] = d
        # Bei Duplikat: behalte ersten Eintrag (chunk-Grenzen können Overlap haben)
    deduped = sorted(days_by_date.values(), key=lambda d: d.get('datum', ''))

    if failed_chunks:
        all_warnings.append(
            f"{failed_chunks} von {len(boundaries)} Abschnitten der Flugstundenübersicht "
            f"konnten nicht vollständig gelesen werden — geprüfte Tage trotzdem berücksichtigt."
        )

    print(f"[DP-Chunked-v10.4.1] FERTIG: {len(deduped)} Tage (von {len(all_days)} pre-dedupe), "
          f"{failed_chunks} fehlgeschlagene Chunks, {cache_hits} cache hits")
    _heartbeat_phase(job_id, 'dp_merge_complete',
                     {'days': len(deduped), 'cache_hits': cache_hits,
                      'label': 'Auswertung wird zusammengeführt…'})

    if not deduped:
        # Alle Chunks failed — wir haben gar nichts
        return None

    return {'days': deduped, 'warnings': all_warnings, '_chunked_v104': True,
            '_chunks_total': len(boundaries), '_chunks_failed': failed_chunks}


def _count_deterministic(structured_days):
    """Zählt aus structured_days deterministisch:
    - hotel_naechte = Σ overnight_after_day
    - arbeitstage = Σ activity_type ∉ {frei, urlaub, krank, none}
    - fahr_tage = Σ Fahrtag-Konstellationen (siehe Logik unten)
    - fahrtage_detail: Liste mit Begründung pro Fahrtag

    Liefert dict {hotel_naechte, arbeitstage, fahr_tage, fahrtage_detail}.
    """
    if not structured_days or not structured_days.get('days'):
        return {'hotel_naechte': 0, 'arbeitstage': 0, 'fahr_tage': 0, 'fahrtage_detail': []}
    days = structured_days['days']

    hotel_naechte = sum(1 for d in days if d.get('overnight_after_day'))

    NICHT_AT = {'frei', 'urlaub', 'krank', 'unknown', 'none'}
    arbeitstage = sum(1 for d in days if d.get('activity_type') not in NICHT_AT)

    # Fahrtage: deterministische Regeln (für reduziertes Schema v6.0.2)
    # Ein Fahrtag entsteht wenn der User HEUTE von ZUHAUSE zur Homebase fährt:
    #   - Same-Day-Trip       → 1 Fahrtag (zur Homebase + zurück abends)
    #   - Office/Training-Tag → 1 Fahrtag
    #   - Tour-Tag wo Vortag KEINE Übernachtung war → 1 Fahrtag (Anreise zur Homebase)
    #   - Tour-Tag wo Vortag Übernachtung war       → 0 (User ist noch unterwegs/kommt heim)
    #   - Standby zuhause → 0 (User ist zuhause)
    fahrtage_detail = []
    days_sorted = sorted(days, key=lambda x: x.get('datum', ''))
    for i, d in enumerate(days_sorted):
        at = d.get('activity_type', '')
        datum = d.get('datum', '')
        if at in NICHT_AT or at == 'standby':
            continue
        if at == 'office':
            fahrtage_detail.append({'datum': datum, 'grund': 'Office am Homebase', 'counted': True})
            continue
        if at == 'training':
            # Training: 1 Fahrtag wenn Vortag NICHT auch training mit Übernachtung
            prev = days_sorted[i-1] if i > 0 else None
            if prev and prev.get('activity_type') == 'training' and prev.get('overnight_after_day'):
                continue  # mehrtägige Schulung, gestern schon angefahren
            fahrtage_detail.append({'datum': datum, 'grund': 'Training Anreise', 'counted': True})
            continue
        if at == 'same_day':
            fahrtage_detail.append({'datum': datum, 'grund': 'Same-Day Tagestrip', 'counted': True})
            continue
        if at == 'tour':
            # Tour-Anreise nur wenn Vortag KEINE Übernachtung
            prev = days_sorted[i-1] if i > 0 else None
            if prev and prev.get('overnight_after_day'):
                continue  # User ist gestern auswärts geblieben → kein neuer Fahrtag heute
            fahrtage_detail.append({'datum': datum, 'grund': 'Tourstart ab Homebase', 'counted': True})
            continue
    fahr_tage = len(fahrtage_detail)

    return {
        'hotel_naechte': hotel_naechte,
        'arbeitstage': arbeitstage,
        'fahr_tage': fahr_tage,
        'fahrtage_detail': fahrtage_detail,
    }


def _opus_classify_structured_days_v6(structured_days, se_summary, year=2025, homebase='FRA', feedback=None):
    """v6.0: Opus klassifiziert NUR steuerlich (Z72/Z73/Z76/Office/Standby/Frei).
    Bekommt strukturierte Tagesdaten als JSON, keine PDFs mehr.
    Ändert KEINE Lese-Fakten (has_fl, layover_ort, etc.) — nur klass + begruendung.
    """
    if not ANTHROPIC_KEY or not structured_days or not structured_days.get('days'):
        return None
    days = structured_days['days']

    classify_tool = {
        'name': 'submit_v6_classifications',
        'description': 'Pro Tag eine steuerliche Klassifikation Z72/Z73/Z74/Z76/Office/Standby/Frei',
        'input_schema': {
            'type': 'object',
            'required': ['classifications', 'nachweis'],
            'properties': {
                'classifications': {
                    'type': 'array',
                    'description': 'Pro Tag mit dienstlicher Aktivität: datum + klass + begruendung. NICHT für FREI/Urlaub/Krank.',
                    'items': {
                        'type': 'object',
                        'required': ['datum', 'klass', 'begruendung'],
                        'properties': {
                            'datum': {'type': 'string'},
                            'klass': {'type': 'string', 'enum': ['Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Standby', 'Frei', 'Sonstiges']},
                            'begruendung': {'type': 'string', 'description': 'Kurze fachliche Begründung'},
                        }
                    }
                },
                'nachweis': {'type': 'string', 'description': 'Monatlicher Nachweis 1-3 Sätze pro Monat'},
                'unklare_tage': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Tage die nicht eindeutig waren'},
            }
        }
    }

    # Wissens-Buch laden
    wissensbuch = ''
    try:
        ref_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_faelle.txt')
        if os.path.exists(ref_path):
            with open(ref_path, encoding='utf-8') as f:
                wissensbuch = f.read()
    except: pass

    z77 = float((se_summary or {}).get('z77_total', 0) or 0)
    auslandsspesen_se = float((se_summary or {}).get('auslandsspesen_total', 0) or 0)

    days_json = json.dumps(days, ensure_ascii=False, default=str)

    prompt = f"""Du bist erfahrener Werbungskosten-Klassifikator für Lufthansa-Kabinenpersonal (Homebase {homebase}, {year}).

═════ DU BEKOMMST STRUKTURIERTE TAGESDATEN — KEINE PDFs! ═══════════════════

Sonnet hat den Dienstplan + Einsatzplan bereits strukturiert ausgelesen.
Deine Aufgabe ist NUR die steuerliche Klassifikation pro Tag.

REGELN:
- Du darfst KEINE Lese-Fakten ändern (has_fl, layover_ort, layover_inland,
  routing, overnight_after_day) — die sind aus dem Dokument extrahiert.
- Du klassifizierst nur: Z72 / Z73 / Z74 / Z76 / Office / Standby / Frei.
- Du musst die Lese-Fakten KONSISTENT verwenden (siehe unten).

═════ KLASSIFIKATIONS-REGELN (KONSISTENZ-PFLICHT) ═══════════════════════════

Bei has_fl=true ODER overnight_after_day=true:
  → KEIN Z72 möglich (es gibt eine Übernachtung außer Homebase).
  → layover_inland=true → Z73-Kontext (Inland-Übernachtung)
  → layover_inland=false → Z76-Kontext (Auslands-Übernachtung)

Bei has_fl=false UND overnight_after_day=false:
  → Z72-Kandidat (wenn activity_type=same_day, A+E gleicher Tag)
  → ODER Office (wenn activity_type=office)
  → ODER Standby (wenn activity_type=standby)

Bei activity_type=schulung_inland_hotel:
  → Z73 für Anreise- und Abreisetag
  → Mittlere Tage: kein VMA, nur Arbeitstag

Bei activity_type=mixed_handover_sameday:
  → Klassifiziere nach dem dominanten Fall — typisch Z76- oder Z73-Abreise
    (von Vortag-Tour). Same-Day-Komponente in der begruendung erwähnen,
    aber kein zusätzliches Z72 (Tag hat bereits VMA aus Tour-Abreise).

═════ KONTEXT ═══════════════════════════════════════════════════════════════

Z77 (LH stfrei gezahlt): {z77:.2f}€
Auslandsspesen-SE:        {auslandsspesen_se:.2f}€

Z76-Plausibilität: ähnlich Auslandsspesen-SE ±30%.

═════ WISSENS-BUCH (Decision-Tree, Anti-Pattern, EStG-Bezug) ════════════════

{wissensbuch[:30000]}

═════ TAGESDATEN ════════════════════════════════════════════════════════════

{days_json}

═════ DEINE AUFGABE ════════════════════════════════════════════════════════

Liefere via Tool 'submit_v6_classifications':
1. classifications: pro Tag mit Aktivität (Tour/Office/Standby/Schulung) ein
   Eintrag mit datum + klass + begruendung
2. nachweis: monatliche Zusammenfassung
3. unklare_tage: Tage die nicht eindeutig waren

WICHTIG:
- Konsistenz mit Lese-Fakten ist PFLICHT (Z72 nur wenn overnight=false UND
  has_fl=false; Z73 nur wenn layover_inland=true; Z76 nur wenn layover_inland=false)
- Bei Mehrtages-Schulungen: erste+letzte Tag als Z73, mittlere als Office
- Bei Multi-Stop-Touren: jeden Tag einzeln nach layover_ort klassifizieren"""

    if feedback and feedback.get('issues'):
        prompt += "\n\n═════ KORREKTUR-AUFTRAG aus Self-Reflection ═════\n"
        for iss in feedback['issues']:
            prompt += f"  • {iss}\n"
        prompt += "\nKorrigiere die spezifischen Probleme. Behalte konsistente Lese-Fakten."

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=300.0)
    import time as _t
    start = _t.time()
    try:
        resp = client.messages.create(
            model='claude-opus-4-7', max_tokens=12000,
            tools=[classify_tool],
            tool_choice={'type': 'tool', 'name': 'submit_v6_classifications'},
            messages=[{'role': 'user', 'content': prompt}]
        )
        elapsed = _t.time() - start
        tool_input = None
        for block in resp.content:
            btype = getattr(block, 'type', None) if not isinstance(block, dict) else block.get('type')
            bname = getattr(block, 'name', None) if not isinstance(block, dict) else block.get('name')
            if btype == 'tool_use' and (bname == 'submit_v6_classifications' or bname is None):
                if isinstance(block, dict):
                    tool_input = block.get('input')
                else:
                    tool_input = getattr(block, 'input', None)
                break
        if not tool_input:
            return None
        # Robust: tool_input könnte pydantic-Model sein
        tool_input = _ensure_dict(tool_input) if not isinstance(tool_input, dict) else tool_input

        # Diagnostic-Logging vor Normalisierung — hilft beim nächsten Bug
        classifications_raw = tool_input.get('classifications', []) if isinstance(tool_input, dict) else []
        raw_type = type(classifications_raw).__name__
        if isinstance(classifications_raw, list) and classifications_raw:
            first_type = type(classifications_raw[0]).__name__
            print(f"[Opus-v6] raw classifications type=list[{first_type}] len={len(classifications_raw)}")
        elif isinstance(classifications_raw, dict):
            print(f"[Opus-v6] raw classifications type=dict keys[:5]={list(classifications_raw.keys())[:5]}")
        else:
            print(f"[Opus-v6] raw classifications type={raw_type} preview={str(classifications_raw)[:100]}")

        # Robuste Normalisierung — handelt list[dict], dict[datum→dict],
        # list[tuple], pydantic-Models, etc.
        classifications = _normalize_v6_classifications(classifications_raw)
        classifications = [c for c in classifications if c.get('datum')]
        nachweis = str(tool_input.get('nachweis', '') or '')
        unklare_raw = tool_input.get('unklare_tage', []) or []
        unklare = [str(u) if not isinstance(u, str) else u for u in unklare_raw]
        pass_label = '[Opus-v6-RECHECK]' if feedback else '[Opus-v6]'
        print(f"{pass_label} {elapsed:.1f}s: {len(classifications)} Klassifikationen, "
              f"{len(unklare)} unklar")
        return {'classifications': classifications, 'nachweis': nachweis, 'unklare_tage': unklare}
    except Exception as e:
        print(f"[Opus-v6] fail: {type(e).__name__}: {str(e)[:200]}")
        return None


def _validate_opus_against_structure(classifications, structured_days):
    """Prüft Opus-Klassifikation gegen harte Lese-Fakten von Sonnet.
    Liefert Liste konkreter Issues bei Konsistenz-Verletzung."""
    if not classifications or not structured_days or not structured_days.get('days'):
        return []
    by_date = {d.get('datum'): d for d in structured_days['days']}
    issues = []
    for c in classifications:
        datum = c.get('datum')
        klass = c.get('klass')
        d = by_date.get(datum)
        if not d:
            continue
        overnight = d.get('overnight_after_day')
        has_fl = d.get('has_fl')
        layover_inland = d.get('layover_inland')
        if klass == 'Z72' and (overnight or has_fl):
            issues.append(f"Z72 am {datum} unmöglich: overnight={overnight}, has_fl={has_fl}")
        if klass == 'Z76' and layover_inland is True:
            issues.append(f"Z76 am {datum} prüfen: layover_inland=true (Inland-Layover)")
        if klass == 'Z73' and layover_inland is False:
            issues.append(f"Z73 am {datum} prüfen: layover_inland=false (Auslands-Layover)")
        if klass in ('Z73', 'Z76') and not (overnight or has_fl):
            issues.append(f"{klass} am {datum} ohne Übernachtung: overnight=false, has_fl=false")
    return issues


def _aggregate_v6_classification(classifications, structured_days, year=2025):
    """Aggregiert die Pro-Tag-Klassifikationen zu Z72/Z73/Z74/Z76-Summen.
    Z76 in EUR wird mit BMF-Auslandspauschalen pro Land berechnet."""
    bmf = BMF_INLAND_BY_YEAR.get(year, BMF_INLAND_BY_YEAR[2025])
    z72_tage = sum(1 for c in classifications if c.get('klass') == 'Z72')
    z73_tage = sum(1 for c in classifications if c.get('klass') == 'Z73')
    z74_tage = sum(1 for c in classifications if c.get('klass') == 'Z74')

    # Z76 aus BMF-Auslandspauschalen: pro Z76-Tag das Land bestimmen via layover_ort.
    # Defensive: An-/Abreise-Logik via overnight_after_day NUR auf TOUR-Tage anwenden.
    # Office/Training/Standby haben auch overnight=false, sind aber keine "Heimkehr-Tage"
    # — würde sonst falsche An/Abreise-Pauschalen geben.
    by_date = {d.get('datum'): d for d in (structured_days or {}).get('days', [])}
    days_sorted = sorted((structured_days or {}).get('days', []), key=lambda x: x.get('datum', ''))
    idx_by_date = {d.get('datum'): i for i, d in enumerate(days_sorted)}
    z76_eur = 0.0
    for c in classifications:
        if c.get('klass') != 'Z76':
            continue
        d = by_date.get(c.get('datum'))
        if not d:
            continue
        at = d.get('activity_type', '')
        layover_code = d.get('layover_ort', '') or ((d.get('routing') or [''])[-1] if d.get('routing') else '')
        bmf_aus = _get_bmf_for_iata(layover_code, year)

        # An-/Abreise-Erkennung NUR bei activity_type='tour' — sonst Volltag-Satz konservativ
        if at == 'tour':
            i = idx_by_date.get(c.get('datum'))
            prev = days_sorted[i-1] if i and i > 0 else None
            # Anreise-Tag: Vortag war NICHT auf Tour (Vortag overnight=false oder kein Vortag-Tour)
            prev_is_tour_overnight = (
                prev and prev.get('activity_type') == 'tour' and prev.get('overnight_after_day')
            )
            is_anreise = not prev_is_tour_overnight
            # Abreise-Tag: heute overnight=false (User kommt heute heim)
            is_abreise = not d.get('overnight_after_day')
            is_an_ab = is_anreise or is_abreise
        else:
            # Defensive: Z76 bei Nicht-Tour-Tag (z.B. Opus klassifiziert Schulung als Z76)
            # → konservativer Volltag-Satz, keine An/Abreise-Pauschale
            is_an_ab = False

        if bmf_aus:
            satz = bmf_aus.get('an_abreise', 0) if is_an_ab else bmf_aus.get('voll_24h', 0)
            z76_eur += float(satz or 0)
        else:
            # Fallback: 28€ Volltag-Satz wenn Land nicht erkannt
            z76_eur += 28.0

    return {
        'z72_tage': z72_tage,
        'z73_tage': z73_tage,
        'z74_tage': z74_tage,
        'z76_eur': round(z76_eur, 2),
        'z72_eur': round(z72_tage * bmf['tagestrip_8h'], 2),
        'z73_eur': round(z73_tage * bmf['an_abreise'], 2),
        'z74_eur': round(z74_tage * bmf['voll_24h'], 2),
    }


def choose_z77_source(daily_lines=0.0, monthly_z77_list=None, declared_total=0.0):
    """v10.3: Konsolidiert die drei Z77-Quellen aus dem SE-PDF zu einer Wahrheit.

    Priorität:
      A) daily_lines  = Σ aller Tageszeilen-stfrei-Werte. Bevorzugt — ignoriert
         SUMME-Zeilen, ist die mathematisch sauberste Quelle.
      B) monthly_sum  = Σ monatliche_z77[].z77_monat. Nutzen wenn daily fehlt.
      C) declared_total = Sonnet's z77_total-Pick aus einer SUMME-Zeile. Nur
         Fallback und nie blind vertrauen — Sonnet greift gerne die FALSCHE
         SUMME-Zeile (Monats-statt Jahresdaten).

    Liefert: dict mit
      - z77_used:       finaler Z77-Wert
      - z77_source:     'daily_lines' | 'monthly_sum' | 'declared_total' | 'none'
      - z77_audit:      vollständiger Cross-Check (used, source, alle 3 Quellen,
                        diff_daily_monthly, notes-Liste)
      - warnings:       nur echte Warnings (large mismatch, suspicious low/high,
                        keine Quelle). KEIN Lärm bei kleinen, erklärbaren Diffs.

    Toleranz: max(50€, 2%) zwischen daily und monthly = INFO-Note, keine Warning.
    """
    monthly_list = monthly_z77_list or []
    monthly_sum = round(sum(float(m.get('z77_monat', 0) or 0) for m in monthly_list), 2)
    daily = round(float(daily_lines or 0), 2)
    declared = round(float(declared_total or 0), 2)

    notes = []
    warnings = []

    # ── Priorität A: daily_lines (bevorzugt) ──
    if daily > 0:
        used = daily
        source = 'daily_lines'
        if monthly_sum > 0:
            diff = abs(daily - monthly_sum)
            tol = max(50.0, daily * 0.02)
            if diff <= tol:
                notes.append(
                    f"daily_lines={daily:.2f} ✓ monthly_sum={monthly_sum:.2f} "
                    f"(diff {diff:.2f} ≤ tolerance {tol:.2f})"
                )
            else:
                warnings.append(
                    f"Z77 daily/monthly mismatch: daily={daily:.2f} "
                    f"monthly={monthly_sum:.2f} diff={diff:.2f}"
                )
        if declared > 0 and abs(declared - used) > max(100.0, used * 0.05):
            notes.append(
                f"declared_total={declared:.2f} ignored — daily_lines={daily:.2f} "
                f"is the trusted source"
            )
    # ── Priorität B: monthly_sum (wenn daily fehlt) ──
    elif monthly_sum > 0:
        used = monthly_sum
        source = 'monthly_sum'
        if declared > 0 and abs(declared - monthly_sum) > max(100.0, monthly_sum * 0.05):
            notes.append(
                f"declared_total={declared:.2f} ignored; monthly_sum={monthly_sum:.2f} "
                f"appears more complete"
            )
    # ── Priorität C: declared_total (nur Fallback) ──
    elif declared > 0:
        used = declared
        source = 'declared_total'
        warnings.append(
            f"Z77 only declared_total available ({declared:.2f}) — "
            f"keine Tageszeilen- oder Monats-Aufschlüsselung verfügbar"
        )
    # ── Keine Quelle ──
    else:
        used = 0.0
        source = 'none'
        warnings.append("Keine Z77-Quelle verfügbar — Steuerfrei-Spesen = 0")

    # Plausi-Checks (typisches Vollzeit-Crew-Z77: 3000–7000€)
    if 0 < used < 1500:
        warnings.append(
            f"Z77 verdächtig niedrig ({used:.2f}) — typisch 3000-7000€ "
            f"für Vollzeit-Crew. Möglicherweise wurden Monate nicht erfasst."
        )
    elif used > 10000:
        warnings.append(
            f"Z77 verdächtig hoch ({used:.2f}) — typisch 3000-7000€. "
            f"Möglicherweise doppelt summiert."
        )

    return {
        'z77_used': used,
        'z77_source': source,
        'z77_audit': {
            'used': used,
            'source': source,
            'daily_lines': daily,
            'monthly_sum': monthly_sum,
            'declared_total': declared,
            'diff_daily_monthly': round(abs(daily - monthly_sum), 2) if (daily > 0 and monthly_sum > 0) else None,
            'notes': notes,
        },
        'warnings': warnings,
    }


def _get_bmf_for_iata(iata, year, _diag=None):
    """Hilfsfunktion: BMF-Auslandspauschale für einen IATA-Code.
    Liefert dict {voll_24h, an_abreise} oder None.

    v8.6: optional `_diag`-dict mit Listen 'bmf_missing'/'iata_unknown' —
    wird befüllt wenn Mapping fehlt, damit der Caller die Diagnose
    bekommt (statt still 0 zu liefern).
    """
    if not iata:
        return None
    iata_upper = iata.upper().strip()
    if _is_inland_code(iata_upper):
        return None  # Inland — keine Auslandspauschale
    try:
        from bmf_data import IATA_TO_BMF, BMF_AUSLAND_BY_YEAR
        land = IATA_TO_BMF.get(iata_upper)
        if not land:
            if _diag is not None:
                _diag.setdefault('iata_unknown', []).append(iata_upper)
            return None
        bmf_year = BMF_AUSLAND_BY_YEAR.get(year) or BMF_AUSLAND_BY_YEAR.get(2025)
        raw = bmf_year.get(land)
        if not raw:
            if _diag is not None:
                _diag.setdefault('bmf_missing', []).append({'iata': iata_upper, 'land': land, 'year': year})
            return None
        if isinstance(raw, tuple) and len(raw) >= 2:
            return {'voll_24h': float(raw[0]), 'an_abreise': float(raw[1])}
        if isinstance(raw, dict):
            return raw
        return None
    except Exception:
        return None


def _sonnet_read_se_structured(pdf_bytes_list, year=2025):
    """v7.0: Sonnet liest SE-PDFs strukturiert PRO ZEILE (jede Tour-/Tag-Zeile).
    Liefert Liste von SE-Zeilen mit datum, stfrei-Betrag, stfrei-Ort, Zwölftel,
    Storno-Flag. Backend nutzt das als steuerlichen Anker für Z72/Z73/Z76.

    SE-Format pro Zeile (typische Lufthansa-Streckeneinsatz-Abrechnung):
      DATUM | AB | AN | SPESEN-€ | ORT | ZWÖLFTEL | STFREI-€ | STFREI-ORT |
      STEUER | WERBKO | DOPP | STORNO

    Output:
    {
      "se_lines": [
        {
          "datum": "2025-03-04",
          "stfrei_betrag": 30.00,
          "stfrei_ort": "BLR",
          "stfrei_inland": false,
          "zwoelftel": 12,         # 12 = ganzer Tag, 1-11 = anteilig
          "storno": false,
          "gesamt": 60.00,
          "steuerpflichtig": 30.00,
          "ort_routing": "FRA-BLR"  # Optional: wenn ablesbar
        },
        ...
      ],
      "z77_total": 4655.00,
      "warnings": [...]
    }
    """
    if not pdf_bytes_list or not ANTHROPIC_KEY:
        return None
    pdf_bytes_list = _bytes_list(pdf_bytes_list)
    if not pdf_bytes_list:
        return None

    se_struct_tool = {
        'name': 'submit_se_lines',
        'description': 'Liefere alle SE-Zeilen strukturiert pro Datum — keine steuerliche Klassifikation.',
        'input_schema': {
            'type': 'object',
            'required': ['se_lines'],
            'properties': {
                'se_lines': {
                    'type': 'array',
                    'description': f'Eine Zeile pro Datum mit dienstlicher Aktivität in {year}. Storno-Zeilen MIT storno=true mitliefern (NICHT weglassen — Backend filtert).',
                    'items': {
                        'type': 'object',
                        'required': ['datum'],
                        'properties': {
                            'datum': {'type': 'string', 'description': 'YYYY-MM-DD'},
                            'stfrei_betrag': {'type': 'number', 'description': 'Steuerfrei-Betrag aus Spalte STFREI-€ (0 wenn keiner)'},
                            'stfrei_ort': {'type': 'string', 'description': '3-Letter-IATA-Code aus Spalte STFREI-ORT (z.B. FRA, BLR, MUC) oder leer'},
                            'stfrei_inland': {'type': 'boolean', 'description': 'true wenn stfrei_ort einer von FRA/MUC/HAM/DUS/STR/CGN/HAJ/BER/LEJ/NUE/BRE'},
                            'zwoelftel': {'type': 'integer', 'description': 'Spalte ZWÖLFTEL: 12 = ganzer Tag, 1-11 = anteilig'},
                            'storno': {'type': 'boolean', 'description': 'true wenn STORNO-Spalte X enthält oder Zeile durchgestrichen ist'},
                            'gesamt': {'type': 'number', 'description': 'Spalte GESAMT/SPESEN-€ (gesamter Spesen-Betrag)'},
                            'steuerpflichtig': {'type': 'number', 'description': 'Spalte STEUER (steuerpflichtiger Anteil)'},
                            'ort_routing': {'type': 'string', 'description': 'Optional: Routing wenn aus AB/AN ablesbar, z.B. FRA-BLR'},
                        }
                    }
                },
                'z77_total': {'type': 'number', 'description': 'Σ aller stfrei_betrag der Tageszeilen wo storno=false. WICHTIG: IGNORIERE die SUMME-Zeilen im PDF — das sind oft mehrere (Monats- und/oder Jahres-Summen). Summiere immer die Tageszeilen-stfrei-Werte SELBST. Niemals eine beliebige SUMME-Zeile als z77_total verwenden.'},
                'warnings': {'type': 'array', 'items': {'type': 'string'}, 'description': 'Lese-Warnungen z.B. unklare Spalten, fehlende Werte'},
            }
        }
    }

    content = []
    for pdf_bytes in pdf_bytes_list[:14]:
        try:
            content.append({
                'type': 'document',
                'source': {'type': 'base64', 'media_type': 'application/pdf',
                           'data': base64.b64encode(pdf_bytes).decode()}
            })
        except: pass
    if not content:
        return None

    prompt = f"""Du bist ein DOKUMENTEN-PARSER für Lufthansa-Streckeneinsatz-Abrechnungen ({year}).

═════ ZWINGEND: TOOL VERWENDEN ══════════════════════════════════════════════

Du MUSST das Tool 'submit_se_lines' aufrufen. Antworte NICHT mit Freitext.

═════ AUFGABE ════════════════════════════════════════════════════════════════

Lies ALLE Zeilen aus den SE-Abrechnungen Tag-für-Tag. Liefere pro Zeile:
  • datum (YYYY-MM-DD)
  • stfrei_betrag (Spalte STFREI-€)
  • stfrei_ort (Spalte STFREI-ORT, z.B. FRA, BLR, MUC)
  • stfrei_inland (true wenn FRA/MUC/HAM/DUS/STR/CGN/HAJ/BER/LEJ/NUE/BRE)
  • zwoelftel (Spalte ZWÖLFTEL, typisch 12 = ganzer Tag)
  • storno (true wenn X-Marker oder durchgestrichen)
  • gesamt (gesamter Spesen-Betrag)
  • steuerpflichtig (steuerpflichtiger Anteil)
  • ort_routing (z.B. "FRA-BLR" wenn AB/AN-Spalten ablesbar)

═════ STORNO ══════════════════════════════════════════════════════════════════

Storno-Zeilen MIT storno=true mitliefern. Backend filtert sie selbst.
Storno-Indizien: X-Marker in STORNO-Spalte, durchgestrichene Zeile,
mehrere Zeilen pro selben Datum (eine echte + eine Storno).

═════ Z77-VERIFIKATION ════════════════════════════════════════════════════════

Berechne z77_total = Σ stfrei_betrag wo storno=false (Tageszeilen-Summe).
WICHTIG: Es gibt mehrere SUMME-Zeilen im PDF (Monats-Summe, manchmal Jahres-Summe).
IGNORIERE diese SUMME-Zeilen. Summiere immer die Tageszeilen selbst.
Die Tageszeilen-Summe ist die einzige zuverlässige Quelle für z77_total.

═════ KOMPAKTHEIT ═════════════════════════════════════════════════════════════

Halte ort_routing kurz (max 30 Zeichen). Bei ~150-250 Zeilen über 12 Monate
sollte der Output in 32k Tokens passen.

LIEFERE jetzt via Tool die strukturierten SE-Zeilen."""

    content.append({'type': 'text', 'text': prompt})

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=300.0)
    import time as _t
    start = _t.time()
    try:
        resp = None
        for attempt in range(2):
            try:
                resp = client.messages.create(
                    model='claude-sonnet-4-6', max_tokens=32000,
                    tools=[se_struct_tool],
                    tool_choice={'type': 'tool', 'name': 'submit_se_lines'},
                    messages=[{'role': 'user', 'content': content}]
                )
                break
            except Exception as e:
                if attempt == 1: raise
                print(f"[Sonnet-SE-Structured] retry: {str(e)[:100]}")
                _t.sleep(5)
        elapsed = _t.time() - start
        stop_reason = getattr(resp, 'stop_reason', None) if resp else 'no_response'
        usage = getattr(resp, 'usage', None) if resp else None
        if usage:
            in_tok = getattr(usage, 'input_tokens', '?')
            out_tok = getattr(usage, 'output_tokens', '?')
            print(f"[Sonnet-SE-Structured] resp stop={stop_reason} in_tok={in_tok} out_tok={out_tok}")
        tool_input = None
        for block in (resp.content if resp else []):
            btype = getattr(block, 'type', None) if not isinstance(block, dict) else block.get('type')
            bname = getattr(block, 'name', None) if not isinstance(block, dict) else block.get('name')
            if btype == 'tool_use' and (bname == 'submit_se_lines' or bname is None):
                tool_input = block.get('input') if isinstance(block, dict) else getattr(block, 'input', None)
                if tool_input:
                    break
        if not tool_input:
            print(f"[Sonnet-SE-Structured] kein tool_input — stop={stop_reason}")
            return None
        tool_input = _ensure_dict(tool_input) if not isinstance(tool_input, dict) else tool_input
        se_lines_raw = tool_input.get('se_lines', []) or []
        se_lines = _normalize_v6_classifications(se_lines_raw)
        se_lines = [s for s in se_lines if s.get('datum')]
        # layover_inland aus stfrei_ort ableiten falls nicht gesetzt
        for s in se_lines:
            if 'stfrei_inland' not in s or s.get('stfrei_inland') is None:
                ort = s.get('stfrei_ort', '')
                s['stfrei_inland'] = _is_inland_code(ort) if ort else None
            s.setdefault('storno', False)
            s.setdefault('zwoelftel', 12)
            s.setdefault('stfrei_betrag', 0)
            s.setdefault('stfrei_ort', '')
        z77_total = float(tool_input.get('z77_total', 0) or 0)
        warnings = [str(w) for w in (tool_input.get('warnings', []) or [])]
        non_storno = [s for s in se_lines if not s.get('storno')]
        z77_calc = sum(float(s.get('stfrei_betrag', 0) or 0) for s in non_storno)
        # v10.3: choose_z77_source — daily_lines (z77_calc) ist hier die bevorzugte
        # Quelle. declared_total (z77_total von Sonnet) nur als Cross-Check.
        z77_choice = choose_z77_source(daily_lines=z77_calc,
                                       monthly_z77_list=None,
                                       declared_total=z77_total)
        # Info-Level Log statt WARNUNG, wenn alles im Toleranz-Bereich
        for w in z77_choice['warnings']:
            print(f"[Sonnet-SE-Structured] ⚠ {w}")
        for n in z77_choice['z77_audit']['notes']:
            print(f"[Sonnet-SE-Structured] info: {n}")
        print(f"[Sonnet-SE-Structured] {elapsed:.1f}s: {len(se_lines)} Zeilen "
              f"({len(non_storno)} aktiv, {len(se_lines)-len(non_storno)} Storno) — "
              f"z77={z77_choice['z77_used']:.2f}€ src={z77_choice['z77_source']}")
        return {
            'se_lines': se_lines,
            'z77_total': z77_choice['z77_used'],
            'z77_audit': z77_choice['z77_audit'],
            'warnings': warnings + z77_choice['warnings'],
        }
    except Exception as e:
        print(f"[Sonnet-SE-Structured] fail: {type(e).__name__}: {str(e)[:200]}")
        return None


# ═════════════════════════════════════════════════════════════════════════════
# v7.0 DETERMINISTIC CLASSIFICATION
# Sonnet liest DP+SE strukturiert. Backend matcht pro Datum + klassifiziert
# deterministisch. Opus nur für unklare Edge-Cases.
# ═════════════════════════════════════════════════════════════════════════════

def _match_dp_se_per_day(structured_days, se_structured, homebase='FRA'):
    """Matcht DP-Tagesdaten + SE-Zeilen pro Kalendertag.
    Liefert Liste von matched_days mit dp + se + initial_klass + needs_opus.
    Storno-SE-Zeilen werden gefiltert.

    v8.1: DP-Tage werden mit Heuristik-Felder (requires_commute,
    is_workday, starts_at_homebase, ends_at_homebase) angereichert,
    falls Sonnet sie nicht geliefert hat.
    """
    if not structured_days or not structured_days.get('days'):
        return []
    dp_days = list(structured_days['days'])
    # Sortiert nach Datum für prev/next-Lookups bei Heuristik
    dp_days_sorted = sorted(dp_days, key=lambda d: d.get('datum', ''))
    # v8.4: Homebase-Logging zu Beginn (Audit-Trail kann Homebase-Wahl belegen)
    homebase_upper = (homebase or 'FRA').upper()
    print(f"[v8-homebase] selected={homebase_upper}")
    # v8.1: Anreicherung mit prev/next-Kontext
    for i, d in enumerate(dp_days_sorted):
        prev_d = dp_days_sorted[i-1] if i > 0 else None
        next_d = dp_days_sorted[i+1] if i+1 < len(dp_days_sorted) else None
        _enrich_dp_with_v8_fields(d, prev_d, next_d, homebase)
        print(f"[v8-dp-detail] datum={d.get('datum','')} activity={d.get('activity_type','')} "
              f"start_homebase={d.get('starts_at_homebase')} end_homebase={d.get('ends_at_homebase')} "
              f"requires_commute={d.get('requires_commute')} is_workday={d.get('is_workday')} "
              f"start={d.get('start_time') or '-'} end={d.get('end_time') or '-'}")
        # v8.4: zusätzliches Detail-Logging gegen Homebase-Vergleich
        routing_str = '-'.join(d.get('routing') or []) or '-'
        print(f"[v8-homebase-check] datum={d.get('datum','')} routing={routing_str} "
              f"starts_at_homebase={d.get('starts_at_homebase')} "
              f"ends_at_homebase={d.get('ends_at_homebase')} "
              f"homebase={homebase_upper}")

    # SE: Datum → liste aktive Zeilen (Storno gefiltert)
    se_by_date = {}
    if se_structured and se_structured.get('se_lines'):
        for s in se_structured['se_lines']:
            if s.get('storno'):
                continue
            datum = s.get('datum')
            if not datum:
                continue
            se_by_date.setdefault(datum, []).append(s)

    matched = []
    for d in dp_days_sorted:
        datum = d.get('datum')
        if not datum:
            continue
        se_for_day = se_by_date.get(datum, [])
        stfrei_total = sum(float(s.get('stfrei_betrag', 0) or 0) for s in se_for_day)
        if se_for_day:
            best = max(se_for_day, key=lambda s: float(s.get('stfrei_betrag', 0) or 0))
            stfrei_ort = best.get('stfrei_ort', '')
            stfrei_inland = best.get('stfrei_inland')
            zwoelftel = best.get('zwoelftel', 12)
        else:
            stfrei_ort = ''
            stfrei_inland = None
            zwoelftel = 0
        matched.append({
            'datum': datum,
            'dp': d,
            'se': {
                'stfrei_total': stfrei_total,
                'stfrei_ort': stfrei_ort,
                'stfrei_inland': stfrei_inland,
                'zwoelftel': zwoelftel,
                'lines': se_for_day,
                'count': len(se_for_day),
            },
        })
    return matched


def _belongs_to_previous_tour(day_match, prev_match, prev_cluster):
    """True wenn day_match ein nachklingender Abreise/Stop-Over-Tag der vorherigen
    Tour ist. Wird nach _build_tour_clusters aufgerufen um Cluster zu erweitern.

    v7.5: Erweitert um Heimkehr-Tag-Erkennung ohne aktive SE — wenn der Cluster
    Auslands-Layover hatte und heute ein 'tour'/'unknown'-Tag OHNE neue Übernachtung
    kommt, ist das fast sicher der Heimkehrtag (BLR/HKG/BOS-Pattern).
    """
    if not prev_match or not prev_cluster:
        return False
    if not day_match:
        return False
    d = day_match['dp']
    se = day_match['se']
    prev_d = prev_match['dp']
    prev_se = prev_match['se']
    # Vortag muss aktive Tour-Übernachtung gewesen sein
    if not prev_d.get('overnight_after_day'):
        return False
    today_at = d.get('activity_type', '')
    today_overnight = bool(d.get('overnight_after_day'))
    # Heutiger Tag mit eigener Klassifikation (nicht-tour Aktivität): nicht anhängen
    if today_at in ('frei', 'urlaub', 'krank', 'standby', 'office', 'training', 'same_day'):
        return False
    has_active_se = se.get('count', 0) > 0 and se.get('stfrei_total', 0) > 0
    if has_active_se:
        # Heutige SE-Ort = Vortag-Layover-Ort? (Abreisetag aus Layover)
        prev_layover = prev_se.get('stfrei_ort') or prev_d.get('layover_ort', '')
        today_se_ort = se.get('stfrei_ort', '')
        if today_se_ort and prev_layover and today_se_ort.upper() == prev_layover.upper():
            return True
        # Heutige SE Auslands-Ort + prev_cluster Auslands-Tour → Abreisetag
        if se.get('stfrei_inland') is False and prev_cluster.get('has_foreign'):
            return True
        # Heutige SE Inland-Ort + prev Auslands-Layover + heute kein overnight → Heimkehr-Tag
        if se.get('stfrei_inland') is True and prev_cluster.get('has_foreign') and not today_overnight:
            return True
    # DP-Routing: beginnt am vorherigen Layover?
    today_routing = d.get('routing') or []
    prev_layover = prev_se.get('stfrei_ort') or prev_d.get('layover_ort', '')
    if today_routing and prev_layover:
        first = (today_routing[0] or '').upper() if today_routing else ''
        if first == prev_layover.upper():
            return True
    # NEU v7.5: Heimkehr-Pattern ohne aktive SE — tour/unknown nach Auslands-Layover
    # ohne neue Übernachtung. Cabin-Crew-Heimkehr von Langstrecken (BLR/HKG/BOS etc.)
    if (not today_overnight) and prev_cluster.get('has_foreign') and today_at in ('tour', 'unknown', 'none', ''):
        return True
    return False


def _build_tour_clusters(sorted_days):
    """Identifiziert zusammenhängende Tour-Sequenzen (Tour-Cluster).
    PHASE 1: Cluster aus tour-Tagen bilden.
    PHASE 2: Cluster nach hinten erweitern wenn Folgetage Abreise-Charakter haben."""
    clusters = []
    current = None

    def _absorb_layover_info(c, m):
        d = m['dp']
        if not d.get('overnight_after_day'):
            return
        se = m['se']
        se_inland = se.get('stfrei_inland')
        layover_ort = d.get('layover_ort', '')
        if se_inland is False:
            c['has_foreign'] = True
        elif layover_ort and not _is_inland_code(layover_ort):
            c['has_foreign'] = True
        elif se_inland is True:
            c['has_inland'] = True
        elif layover_ort and _is_inland_code(layover_ort):
            c['has_inland'] = True

    # PHASE 1: Cluster aus tour-Tagen
    for i, m in enumerate(sorted_days):
        d = m['dp']
        at = d.get('activity_type', '')
        is_tour = (at == 'tour')
        overnight = bool(d.get('overnight_after_day'))

        if is_tour:
            if current is None:
                current = {'indices': [i], 'has_foreign': False, 'has_inland': False,
                           'anreise_idx': i, 'abreise_idx': i}
            else:
                current['indices'].append(i)
                current['abreise_idx'] = i
            _absorb_layover_info(current, m)
            if not overnight:
                clusters.append(current)
                current = None
        else:
            if current is not None:
                clusters.append(current)
                current = None
    if current is not None:
        clusters.append(current)

    # PHASE 2: Cluster nach hinten erweitern — bei nachklingenden Abreise-/Heimkehr-Tagen
    # v7.5: erlaube bis zu 2 frei/urlaub/krank Tage zwischen Cluster-Ende und Heimkehr
    # (DP-Reader liest Layover-Tage manchmal als 'frei' — Heimkehr darf trotzdem anhängen).
    cluster_by_idx = {}
    for c in clusters:
        for i in c['indices']:
            cluster_by_idx[i] = c
    for c in clusters:
        last_idx_in_cluster = c['indices'][-1] if c['indices'] else None
        if last_idx_in_cluster is None:
            continue
        # bis zu 4 Folgetage scannen, frei/urlaub/krank überspringen
        cur_anchor = last_idx_in_cluster
        for offset in range(1, 5):
            check_idx = last_idx_in_cluster + offset
            if check_idx >= len(sorted_days):
                break
            if check_idx in cluster_by_idx:
                break
            day_match = sorted_days[check_idx]
            today_at = day_match['dp'].get('activity_type', '')
            if today_at in ('frei', 'urlaub', 'krank'):
                # Lücke — überspringen, aber nicht abbrechen
                continue
            prev_match = sorted_days[cur_anchor]
            if _belongs_to_previous_tour(day_match, prev_match, c):
                c['indices'].append(check_idx)
                c['abreise_idx'] = check_idx
                cluster_by_idx[check_idx] = c
                _absorb_layover_info(c, day_match)
                cur_anchor = check_idx
                datum = day_match.get('datum', '')
                print(f"[v8-cluster-extend] datum={datum} cluster={c.get('_id', '?')} "
                      f"at={today_at} reason='nachklingender Abreise-/Heimkehr-Tag'")
            else:
                break
    return clusters


def _document_health_check(lsb_data, se_structured, structured_days, year):
    """v8 Document Health Check.
    Liefert {'status': 'green'|'yellow'|'red', 'issues': [...], 'sources': {...}}.
    Wird VOR _deterministic_classify_v7 aufgerufen. red → keine Berechnung,
    klare Fehlermeldung an User.
    """
    issues = []
    sources = {'lsb': 'green', 'se': 'green', 'dp': 'green'}

    # ── LSB-Check ──
    if not lsb_data:
        issues.append({'source': 'LSB', 'severity': 'red', 'reason': 'Lohnsteuerbescheinigung konnte nicht gelesen werden'})
        sources['lsb'] = 'red'
    else:
        brutto = float(lsb_data.get('brutto', 0) or 0)
        z17    = float(lsb_data.get('z17', 0) or 0)
        if brutto <= 0:
            issues.append({'source': 'LSB', 'severity': 'red', 'reason': 'Brutto-Wert (Zeile 3) nicht erkannt'})
            sources['lsb'] = 'red'
        # Z17 fehlt ist OK (manche LSBs haben kein Jobticket)
        if z17 == 0:
            issues.append({'source': 'LSB', 'severity': 'info', 'reason': 'Zeile 17 (Jobticket) ist 0 oder nicht erkannt — falls Jobticket vorhanden, bitte prüfen'})

    # ── SE-Check ──
    if not se_structured or not se_structured.get('se_lines'):
        issues.append({'source': 'SE', 'severity': 'red', 'reason': 'Streckeneinsatz konnte nicht gelesen werden — keine Zeilen erkannt'})
        sources['se'] = 'red'
    else:
        se_lines = se_structured.get('se_lines', [])
        active = [s for s in se_lines if not s.get('storno')]
        storno = [s for s in se_lines if s.get('storno')]
        if not active:
            issues.append({'source': 'SE', 'severity': 'red', 'reason': 'Keine aktiven SE-Zeilen erkannt (alle storniert?)'})
            sources['se'] = 'red'
        else:
            z77_lines = sum(float(s.get('stfrei_betrag', 0) or 0) for s in active)
            # Monate prüfen
            monate = sorted(set(int(s.get('datum', '0000-00-00')[5:7]) for s in active if s.get('datum')))
            if len(monate) < 6:
                issues.append({'source': 'SE', 'severity': 'warning',
                               'reason': f'Nur {len(monate)} Flugmonate erkannt — bei Vollzeit ungewöhnlich (Teilzeit/Mutterschutz/Krank ist OK)'})
                sources['se'] = 'yellow'
            if z77_lines < 500:
                issues.append({'source': 'SE', 'severity': 'warning',
                               'reason': f'Z77-Summe nur {z77_lines:.2f}€ — bei Vollzeit ungewöhnlich'})
                if sources['se'] == 'green':
                    sources['se'] = 'yellow'

    # ── DP-Check ──
    if not structured_days or not structured_days.get('days'):
        issues.append({'source': 'DP', 'severity': 'red', 'reason': 'Flugstundenübersicht konnte nicht gelesen werden — keine Tage erkannt'})
        sources['dp'] = 'red'
    else:
        days = structured_days.get('days', [])
        # Touren erkannt?
        tour_count = sum(1 for d in days if d.get('activity_type') == 'tour')
        # SE-Zeilen ohne DP-Match?
        if se_structured and se_structured.get('se_lines'):
            dp_dates = set(d.get('datum') for d in days)
            se_active_dates = set(s.get('datum') for s in se_structured['se_lines']
                                  if not s.get('storno') and s.get('datum'))
            unmatched = se_active_dates - dp_dates
            if len(unmatched) > 5:
                issues.append({'source': 'DP', 'severity': 'warning',
                               'reason': f'{len(unmatched)} aktive SE-Tage ohne DP-Match — DP unvollständig?'})
                sources['dp'] = 'yellow'
        # v8.18.2: Health-Threshold an DP-Vollständigkeit angepasst.
        # Sonnet liefert seit v8.18 ALLE Kalendertage inkl. Frei — daher ist
        # 360-366 Tage normal (kein warning).
        # < 250 Tage bei vollem Jahr = unvollständig
        # > 366 = Reader-Bug
        if len(days) < 30:
            issues.append({'source': 'DP', 'severity': 'warning',
                           'reason': f'Nur {len(days)} Tage erkannt — DP unvollständig?'})
            if sources['dp'] == 'green':
                sources['dp'] = 'yellow'
        elif 30 <= len(days) < 250:
            issues.append({'source': 'DP', 'severity': 'warning',
                           'reason': f'{len(days)} Tage erkannt — bei Ganzjahresdatei ungewöhnlich wenig (möglicherweise Frei-Tage übersehen)'})
            if sources['dp'] == 'green':
                sources['dp'] = 'yellow'
        elif 250 <= len(days) <= 366:
            # Normal: 360-366 = vollständiges Jahr, 250-359 möglich (Teiljahr)
            # Info-Eintrag, kein warning
            issues.append({'source': 'DP', 'severity': 'info',
                           'reason': f'{len(days)} Kalendertage gelesen inkl. Frei-/Urlaub-/Krank-Tage'})
        elif len(days) > 366:
            issues.append({'source': 'DP', 'severity': 'warning',
                           'reason': f'{len(days)} Tage erkannt — Reader-Bug (Schaltjahr max 366)'})
            if sources['dp'] == 'green':
                sources['dp'] = 'yellow'
        if tour_count == 0 and se_structured and se_structured.get('se_lines'):
            issues.append({'source': 'DP', 'severity': 'warning',
                           'reason': 'Keine Touren erkannt im DP, aber SE hat Zeilen — DP-Reader-Problem?'})
            sources['dp'] = 'yellow'

    # Gesamt-Status
    if any(s == 'red' for s in sources.values()):
        status = 'red'
    elif any(s == 'yellow' for s in sources.values()):
        status = 'yellow'
    else:
        status = 'green'

    print(f"[v8-health] lsb={sources['lsb']} se={sources['se']} dp={sources['dp']} "
          f"status={status} issues={len(issues)}")
    for issue in issues:
        print(f"[v8-health-issue] source={issue['source']} severity={issue['severity']} reason='{issue['reason'][:120]}'")

    return {'status': status, 'issues': issues, 'sources': sources}


def _enrich_dp_with_v8_fields(dp, prev_dp=None, next_dp=None, homebase='FRA'):
    """v8.1: Heuristik-Fallback für DP-Felder, falls Sonnet sie nicht liefert.
    Setzt requires_commute, is_workday, starts_at_homebase, ends_at_homebase
    aus activity_type + routing + has_fl + overnight_after_day.

    NICHT überschreiben wenn Sonnet das Feld bereits gesetzt hat.
    """
    at = dp.get('activity_type', 'unknown')
    overnight = bool(dp.get('overnight_after_day'))
    prev_overnight = bool(prev_dp and prev_dp.get('overnight_after_day'))
    has_fl = bool(dp.get('has_fl'))
    routing = dp.get('routing') or []
    routing_upper = [r.upper() for r in routing if r]
    homebase_upper = (homebase or 'FRA').upper()

    # is_workday
    if 'is_workday' not in dp:
        if at in ('tour', 'same_day', 'office', 'training', 'standby'):
            dp['is_workday'] = True
        elif at in ('frei', 'urlaub', 'krank'):
            dp['is_workday'] = False
        else:
            dp['is_workday'] = False  # unknown ohne Dienstkontext

    # starts_at_homebase
    if 'starts_at_homebase' not in dp:
        if at in ('frei', 'urlaub', 'krank', 'standby'):
            dp['starts_at_homebase'] = False  # kein Routing
        elif at in ('office', 'training') and not prev_overnight:
            dp['starts_at_homebase'] = True
        elif at == 'same_day':
            dp['starts_at_homebase'] = True  # Same-Day startet immer ab Homebase
        elif at == 'tour':
            # Tour-Anreise (= neue Fahrt von zuhause zur Homebase):
            # - prev_overnight=False (Vortag zuhause)
            # - heute overnight=true (Layover-Hotel) ODER Routing zeigt Homebase→Auswärts
            # WICHTIG: Tour-Tag ohne overnight + ohne klares Routing ist mehrdeutig
            # (kann Heimkehrtag sein, der Sonnet als isolierten 'tour'-Tag gelesen hat).
            # In dem Fall KEIN Anreise-Fahrtag — der Heimkehr-Fahrtag wird separat
            # gezählt (ends_at_homebase + prev_overnight oder Recent-Foreign-Cluster).
            if prev_overnight:
                dp['starts_at_homebase'] = False  # Tourfortsetzung
            elif overnight:
                # heute Hotel → klassische Anreise. Routing=Homebase prüfen falls vorhanden,
                # sonst optimistisch True (Cabin-Crew startet meist ab Homebase).
                if routing_upper and routing_upper[0] != homebase_upper:
                    dp['starts_at_homebase'] = False
                else:
                    dp['starts_at_homebase'] = True
            else:
                # Tour ohne overnight + Vortag zuhause: nur dann Anreise wenn Routing
                # klar Homebase→Ausland→Homebase (Same-Day-Tour-Bein) zeigt.
                if routing_upper and routing_upper[0] == homebase_upper and routing_upper[-1] == homebase_upper:
                    dp['starts_at_homebase'] = True  # Same-Day-Tour
                else:
                    dp['starts_at_homebase'] = False  # vermutlich isolierter Heimkehr-Tag
        else:
            dp['starts_at_homebase'] = False

    # ends_at_homebase
    if 'ends_at_homebase' not in dp:
        if at in ('frei', 'urlaub', 'krank', 'standby'):
            dp['ends_at_homebase'] = False
        elif at in ('office', 'training') and not overnight:
            dp['ends_at_homebase'] = True
        elif at == 'same_day':
            dp['ends_at_homebase'] = True
        elif at == 'tour':
            if overnight:
                dp['ends_at_homebase'] = False  # Layover heute
            else:
                # Kein overnight: Heimkehrtag oder Same-Day. Bei klarem Routing prüfen.
                if routing_upper:
                    dp['ends_at_homebase'] = (routing_upper[-1] == homebase_upper)
                else:
                    # Ohne Routing: wenn Vortag overnight war → vermutlich Heimkehr → True
                    # sonst (Vortag auch zuhause) → konservativ False
                    dp['ends_at_homebase'] = bool(prev_overnight)
        else:
            dp['ends_at_homebase'] = False

    # requires_commute (strenger als is_workday)
    if 'requires_commute' not in dp:
        # NUR wenn der Dienst HEUTE NEU von zuhause zur Homebase startet
        if dp.get('is_workday') and dp.get('starts_at_homebase'):
            # Standby zuhause = KEIN commute (kein Weg zur Homebase)
            if at == 'standby':
                dp['requires_commute'] = False
            else:
                dp['requires_commute'] = True
        else:
            dp['requires_commute'] = False

    # duty_duration_minutes (ableiten aus start_time/end_time wenn nicht da)
    if 'duty_duration_minutes' not in dp or not dp.get('duty_duration_minutes'):
        st = dp.get('start_time', '')
        et = dp.get('end_time', '')
        if st and et and ':' in st and ':' in et:
            try:
                sh, sm = int(st.split(':')[0]), int(st.split(':')[1])
                eh, em = int(et.split(':')[0]), int(et.split(':')[1])
                duration = (eh * 60 + em) - (sh * 60 + sm)
                if duration < 0:
                    duration += 24 * 60  # über Mitternacht
                dp['duty_duration_minutes'] = duration
            except (ValueError, IndexError):
                pass

    return dp


# v8.18.3 Klassifikator-Konstanten (Magic Numbers benannt)
EVENING_FOREIGN_TOUR_START_HOUR = 18      # Briefing-Start ab 18:00 → Abend-Anreise
TRAINING_SEQ_MIN_DAYS = 5                  # ≥5 Tage = Mehrtages-Schulungsblock
SAME_DAY_Z72_TOTAL_MINUTES = 480           # 8h (duty + 2× commute) → Same-Day Z72
RECENT_FOREIGN_CLUSTER_MAX_BACK = 4        # Lookback-Tage für Heimkehr-Erkennung

# v8.20.1: Helper für Z72-Plausibilisierung. Unterscheidet Dienst-Zeit
# (braucht +2×commute) von Tour-Abwesenheits-Zeit (bereits inkl. Anfahrt).
def _total_minutes_for_z72(d, commute_minutes):
    """Gibt (total_min, duty_known, source_label) zurück.

    Wenn d['time_is_absence']=True: total_min = duty_duration_minutes (bereits
        Tour-Start→Tour-Ende, KEINE doppelte Fahrzeit).
    Sonst: total_min = duty_duration_minutes + 2 × commute_minutes (Hin+Zurück).

    Wenn duty_duration_minutes None/missing → duty_known=False, total_min=0.
    """
    raw_duty = d.get('duty_duration_minutes')
    duty_known = isinstance(raw_duty, (int, float))
    if not duty_known:
        return 0, False, 'no_duty'
    duty_min = int(raw_duty)
    if d.get('time_is_absence'):
        return duty_min, True, 'absence_time'
    commute_total = (commute_minutes * 2) if commute_minutes > 0 else 0
    return duty_min + commute_total, True, 'duty_plus_commute'


# v8.18.4: Training-Sequenz-Marker-Klassen.
# CLOSED_SEMINAR = zusammenhängender Block ohne tägliche Homebase-Präsenz
#   (klassisches Seminar, Sprachkurs, externer Lehrgang)
#   → Kollaps auf 1 Fahrtag, Folgetage skip
# DAILY_PRESENCE = Marker signalisiert tägliche Homebase-Präsenz
#   (Schulung, Bürodienst, Emergency-Training, Erste Hilfe, technische
#   Schulung, Medical/Sprachtest)
#   → KEIN Kollaps, jeder Tag eigener Fahrtag/Reinigungstag
TRAINING_CLOSED_SEMINAR_FIRST_TOKEN = ('SM',)
TRAINING_CLOSED_SEMINAR_SUBSTRINGS = ('SEMINAR', 'LEHRGANG', 'SPRACHKURS')
TRAINING_DAILY_PRESENCE_FIRST_TOKEN = ('D4', 'EK', 'EM', 'EH', 'TK',
                                        'MEDICAL', 'SPRACHTEST', 'BRIEFING')

# v8.18.3 Feature-Flags (vorerst nicht runtime-toggelbar — nur Benennung der
# Sub-Logiken; spätere Toggles können hier eingehängt werden ohne tiefere Patches)
FEATURE_EVENING_FOREIGN_TOUR_START_TO_Z73 = True
FEATURE_ACTIVE_FOREIGN_SE_ISSUE_RESCUE_TO_Z76 = True
FEATURE_TRAINING_SEQUENCE_COMMUTE_COLLAPSE = True
FEATURE_HOTEL_COUNTER_STRICT_MODE = True
FEATURE_DYNAMIC_HOMEBASE_MODE = True


def _deterministic_classify_v7(matched_days, year=2025, homebase='FRA', commute_minutes=0):
    """v8.1 Backend-Klassifikator: deterministisch aus DP+SE pro Tag.

    Architektur (v7.5):
    1. Tour-Cluster bilden (Phase 1: tour-Tage; Phase 2: Heimkehr-Anhang)
    2. Pro Tag → klass + eur + reason setzen, audit_note (informativ) ODER
       unresolved_reason (echter Issue) optional
    3. Counter aus klass am Ende aggregieren — kein arbeitstage += 1 im Loop
    4. VMA-Unmapped-SE-Liste: aktive SE-Zeile ohne Z72/73/74/76 → echter Issue

    Klassen-Set: Z72, Z73, Z74, Z76, Office, Standby, ZeroDay, Sonstiges
    Counter-Mapping (v8.18.3 — alle aus tage_detail-Flags):
      arbeitstage    = sum(counted_as_workday)
      fahr_tage      = sum(counted_as_fahrtag)
      reinigungstage = sum(counted_as_reinigungstag)
      hotel_naechte  = sum(counted_as_hotel_nacht)
    """
    bmf_inland = BMF_INLAND_BY_YEAR.get(year, BMF_INLAND_BY_YEAR[2025])
    INLAND_TAGESTRIP_8H = bmf_inland['tagestrip_8h']
    INLAND_AN_ABREISE = bmf_inland['an_abreise']
    INLAND_VOLL_24H = bmf_inland['voll_24h']

    sorted_days = sorted(matched_days, key=lambda m: m.get('datum', ''))
    tour_clusters = _build_tour_clusters(sorted_days)
    cluster_for_idx = {}
    for c_id, c in enumerate(tour_clusters):
        c['_id'] = c_id
        for i in c['indices']:
            cluster_for_idx[i] = c
        mixed = c.get('has_foreign') and c.get('has_inland')
        print(f"[v8-cluster] id={c_id} days={len(c['indices'])} "
              f"has_foreign={c.get('has_foreign')} has_inland={c.get('has_inland')} "
              f"mixed={mixed}")

    audit_notes = []        # informativ (z.B. FRA-Stempel bei Auslandstour)
    unresolved_days = []    # echte Issues (klass=Sonstiges mit Grund)
    vma_unmapped_se = []    # aktive SE-Zeile ohne VMA-Klassifikation
    rescues = []            # v8.18.1: Audit-Trail für Issue→Z76-Rescue
    tage_detail = []
    z76_eur = 0.0

    # v8.6: Diagnose-Listen für Audit-Output
    _diag_bmf = {'bmf_missing': [], 'iata_unknown': []}

    def _bmf(iata):
        """Lokaler Wrapper — sammelt BMF-Mapping-Lücken in _diag_bmf."""
        return _get_bmf_for_iata(iata, year, _diag=_diag_bmf)

    def _recent_foreign_cluster(idx, max_back=RECENT_FOREIGN_CLUSTER_MAX_BACK):
        """Schaut bis max_back Tage zurück nach geschlossenem has_foreign Cluster."""
        for back in range(1, max_back + 1):
            bi = idx - back
            if bi < 0:
                return None
            bc = cluster_for_idx.get(bi)
            if bc and bc.get('has_foreign'):
                return bc
        return None

    def _resolve_isolated_tour_day(i, m, d, se):
        """Same-Day/Heimkehr-Auflösung für isolierte Tour-Tage (kein Cluster, kein
        prev_overnight). Liefert (klass, eur, reason, audit_note_or_None,
        unresolved_reason_or_None)."""
        datum = m['datum']
        has_fl = bool(d.get('has_fl'))
        routing = d.get('routing') or []
        has_active_se = se.get('count', 0) > 0 and float(se.get('stfrei_total', 0) or 0) > 0
        se_ort = se.get('stfrei_ort', '')
        se_inland = se.get('stfrei_inland')

        # 1. Recent-Foreign-Cluster Lookback (Heimkehr aus Langstrecke)
        rec = _recent_foreign_cluster(i, max_back=RECENT_FOREIGN_CLUSTER_MAX_BACK)
        if rec:
            target_iata = ''
            for ci in rec.get('indices', []):
                cm = sorted_days[ci]
                if cm['dp'].get('overnight_after_day'):
                    cand = cm['se'].get('stfrei_ort') or cm['dp'].get('layover_ort', '')
                    if cand and not _is_inland_code(cand):
                        target_iata = cand
                        break
            bmf_aus = _bmf(target_iata)
            satz = float((bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0) or 0)
            return ('Z76', satz,
                    f'Heimkehr aus Auslands-Cluster (Layover {target_iata or "?"}) Z76 An/Ab',
                    f'{datum}: tour-Tag isoliert, Heimkehr aus Auslands-Cluster {target_iata or "?"} → Z76',
                    None)

        # 2. Aktive SE-Zeile vorhanden
        if has_active_se and se_ort:
            if se_inland is False:
                bmf_aus = _bmf(se_ort)
                satz = float((bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0) or 0)
                return ('Z76', satz,
                        f'Aktive Auslands-SE {se_ort} (Same-Day) Z76',
                        f'{datum}: aktive Auslands-SE {se_ort} ohne Cluster → Z76 Same-Day',
                        None)
            elif se_inland is True:
                # Inland-Same-Day mit aktiver SE > 8h plausibel
                return ('Z72', INLAND_TAGESTRIP_8H,
                        f'Aktive Inland-SE {se_ort} Same-Day (>8h) Z72',
                        None,
                        None)

        # 3. DP-Indizien: FL-Marker oder Multi-Stop-Routing → >8h plausibel
        if has_fl or len(routing) >= 2:
            return ('Z72', INLAND_TAGESTRIP_8H,
                    f'Tour-Tag isoliert mit FL/Routing — Same-Day Z72',
                    None,
                    None)

        # 4. Sonst: 0€-Day (Arbeitstag aber keine VMA-Pauschale, keine echte Auswärts-Tätigkeit)
        return ('ZeroDay', 0.0,
                'Tour-Tag isoliert, keine FL/SE/Cluster-Spur — kein VMA',
                None,
                None)

    # v8.18.4: Training-Seminar-Sequenz mit Marker-Klassen-Trennung.
    # CLOSED_SEMINAR (SM/SEMINAR/LEHRGANG): kollabiert auf 1 Fahrtag, 1-Tag-Lücke
    #   wird überbrückt.
    # DAILY_PRESENCE (D4/EK/EM/EH/TK/...): jede Tag eigener Fahrtag, KEIN Kollaps,
    #   keine Gap-Toleranz.
    # Echter Flugdienst (has_fl/tour/same_day) bricht jede Sequenz.
    # explicit_daily_commute=true erzwingt Tag-für-Tag-Zählung selbst bei SM.

    def _marker_first_token(d_):
        raw = (d_.get('raw_marker', '') or '').upper().strip()
        return raw.split()[0] if raw else ''

    def _is_closed_seminar_day(m_):
        """SM/SEMINAR-Style: Kollaps-fähig."""
        d_ = m_['dp']
        at_ = d_.get('activity_type', '')
        if d_.get('has_fl') or at_ in ('tour', 'same_day'):
            return False
        if at_ not in ('training', 'office'):
            return False
        first = _marker_first_token(d_)
        raw = (d_.get('raw_marker', '') or '').upper()
        # Daily-Presence-Marker schließt Closed-Seminar aus, auch wenn Substring SEMINAR
        if first in TRAINING_DAILY_PRESENCE_FIRST_TOKEN:
            return False
        if first in TRAINING_CLOSED_SEMINAR_FIRST_TOKEN:
            return True
        if any(tok in raw for tok in TRAINING_CLOSED_SEMINAR_SUBSTRINGS):
            return True
        return False

    def _is_daily_presence_day(m_):
        """D4/EK/EM/EH/TK-Style: tägliche Präsenz, kein Kollaps."""
        d_ = m_['dp']
        at_ = d_.get('activity_type', '')
        if d_.get('has_fl') or at_ in ('tour', 'same_day'):
            return False
        if at_ not in ('training', 'office'):
            return False
        first = _marker_first_token(d_)
        return first in TRAINING_DAILY_PRESENCE_FIRST_TOKEN

    def _is_seminar_day(m_):
        """Sequenz-Mitglied: jede Training/Office-Tag (closed oder daily)."""
        return _is_closed_seminar_day(m_) or _is_daily_presence_day(m_) or (
            m_['dp'].get('activity_type') == 'training'
            and not m_['dp'].get('has_fl')
        )

    def _is_seminar_gap(m_):
        """Tolerable Lücke: Frei/Urlaub/Krank/unknown OHNE echten Flug."""
        d_ = m_['dp']
        at_ = d_.get('activity_type', '')
        if d_.get('has_fl') or at_ in ('tour', 'same_day'):
            return False
        return at_ in ('frei', 'urlaub', 'krank', 'unknown', 'none', '')

    def _finalize_seq(seq_indices_):
        """v8.18.4: schließt eine erkannte Sequenz ab. Setzt training_seq_skip
        nur bei reinem closed_seminar-Block ohne daily_presence/explicit_commute,
        sonst kein Kollaps. Gibt ein Audit-Dict zurück (oder None für unter-Min)."""
        if len(seq_indices_) < TRAINING_SEQ_MIN_DAYS:
            return None
        seq_days_pre = [sorted_days[idx] for idx in seq_indices_]
        any_daily_commute = any(dd['dp'].get('explicit_daily_commute') is True
                                for dd in seq_days_pre)
        all_closed_seminar = all(_is_closed_seminar_day(dd) for dd in seq_days_pre)
        any_daily_presence = any(_is_daily_presence_day(dd) for dd in seq_days_pre)
        marker_types = sorted(set(
            (_marker_first_token(dd['dp']) or dd['dp'].get('activity_type', ''))
            for dd in seq_days_pre
        ))
        start_d = sorted_days[seq_indices_[0]]['datum']
        end_d = sorted_days[seq_indices_[-1]]['datum']
        days_n = len(seq_indices_)

        # Entscheidungsbaum (v8.18.4):
        if any_daily_commute:
            return {
                'start':         start_d, 'end': end_d, 'days': days_n,
                'marker_types':  marker_types,
                'sequence_type': 'daily_training_presence',
                'why_collapsed': False,
                'counted_fahrtage': days_n,
                'skipped_fahrtage': 0,
                'reason':        'Sequenz mit explicit_daily_commute=true — alle Tage zählen',
            }
        if any_daily_presence:
            # D4/EK/EM/EH/TK-Marker im Block → kein Kollaps
            return {
                'start':         start_d, 'end': end_d, 'days': days_n,
                'marker_types':  marker_types,
                'sequence_type': 'daily_training_presence',
                'why_collapsed': False,
                'counted_fahrtage': days_n,
                'skipped_fahrtage': 0,
                'reason':        'Daily-Presence-Marker (D4/EK/EM/EH/TK) im Block — Homebase-Schulung/Bürodienst, jeder Tag einzeln',
            }
        if all_closed_seminar:
            # Reiner SM/SEMINAR-Block ohne Präsenz-Indizien → kollabieren
            for skip_i in seq_indices_[1:]:
                training_seq_skip.add(skip_i)
            return {
                'start':         start_d, 'end': end_d, 'days': days_n,
                'marker_types':  marker_types,
                'sequence_type': 'closed_seminar_block',
                'why_collapsed': True,
                'counted_fahrtage': 1,
                'skipped_fahrtage': days_n - 1,
                'reason':        'Geschlossener Seminarblock (SM/SEMINAR) ohne tägliche Anfahrts-Indizien',
            }
        # Mixed/unklarer Block → konservativ kein Kollaps
        return {
            'start':         start_d, 'end': end_d, 'days': days_n,
            'marker_types':  marker_types,
            'sequence_type': 'office_training_sequence_no_collapse',
            'why_collapsed': False,
            'counted_fahrtage': days_n,
            'skipped_fahrtage': 0,
            'reason':        'Office-/Training-Sequenz ohne klare Closed-Seminar-Marker — konservativ kein Kollaps',
        }

    training_seq_skip = set()
    training_seq_audit = []
    seq_start_pre = None
    seq_indices = []         # alle Indices in der aktuellen Sequenz (ohne Lücken)
    seq_all_closed = True    # v8.18.4: sequence ist bisher pure closed_seminar
    pending_gap = None       # potenzielle Lücke (max 1 Tag)
    i_pre = 0
    while i_pre < len(sorted_days):
        m_pre = sorted_days[i_pre]
        if _is_seminar_day(m_pre):
            if seq_start_pre is None:
                seq_start_pre = i_pre
                seq_indices = [i_pre]
                seq_all_closed = _is_closed_seminar_day(m_pre)
            else:
                seq_indices.append(i_pre)
                if not _is_closed_seminar_day(m_pre):
                    seq_all_closed = False
            pending_gap = None
            i_pre += 1
        elif (seq_start_pre is not None and seq_all_closed
              and _is_seminar_gap(m_pre) and pending_gap is None):
            # v8.18.4: 1-Tag-Lücke nur tolerieren wenn aktuelle Sequenz reines closed_seminar
            pending_gap = i_pre
            i_pre += 1
        else:
            # Sequenz endet hier
            audit = _finalize_seq(seq_indices)
            if audit is not None:
                training_seq_audit.append(audit)
            seq_start_pre = None
            seq_indices = []
            seq_all_closed = True
            pending_gap = None
            i_pre += 1
    # Sequenz am Jahresende
    if seq_start_pre is not None:
        audit = _finalize_seq(seq_indices)
        if audit is not None:
            training_seq_audit.append(audit)

    for i, m in enumerate(sorted_days):
        d = m['dp']
        se = m['se']
        datum = m['datum']
        at = d.get('activity_type', 'unknown')
        overnight = bool(d.get('overnight_after_day'))
        has_fl = bool(d.get('has_fl'))
        prev = sorted_days[i-1] if i > 0 else None
        next_ = sorted_days[i+1] if i+1 < len(sorted_days) else None
        prev_overnight = bool(prev and prev['dp'].get('overnight_after_day'))
        prev_at = prev['dp'].get('activity_type', '') if prev else ''
        next_overnight = bool(next_ and next_['dp'].get('overnight_after_day'))

        # Cluster-Extend: Tag durch _belongs_to_previous_tour angehängt → behandeln als 'tour'
        in_extended_cluster = (i in cluster_for_idx) and at not in ('tour', 'same_day', 'office', 'training', 'standby')
        if in_extended_cluster:
            at = 'tour'

        klass = 'Issue'
        eur_added = 0.0
        reason = ''
        audit_note = None
        unresolved_reason = None
        # v8.14: Z73-Untertyp explizit als Flag setzen (statt aus reason-String parsen).
        # Mögliche Werte:
        #   'evening_foreign_tour_start' — späte Auslandstour-Anreise, Tag in DE,
        #                                   Flugnacht → keine Hotelnacht
        #   'inland_layover'             — echter Inland-Layover/Hotel
        #   ''                            — kein Z73 oder anderer Z73-Subtyp
        z73_type = ''

        if at in ('frei', 'urlaub', 'krank'):
            klass = 'Frei'
            reason = at
            tage_detail.append({
                'datum': datum, 'klass': klass, 'begruendung': reason,
                'marker': d.get('raw_marker', '') or at,
                'routing': '-'.join(d.get('routing') or []),
                'tour_dauer': 1, 'eur': 0.0,
                'dienstlich': False,
                # v8.7: minimale nested Sections auch für Frei-Tage
                'reader_facts': {
                    'datum': datum,
                    'activity_type': at,
                    'marker_raw': d.get('raw_marker', ''),
                    'routing': list(d.get('routing') or []),
                    'has_fl': bool(d.get('has_fl')),
                    'overnight_after_day': bool(d.get('overnight_after_day')),
                    'layover_ort': d.get('layover_ort', '') or '',
                    'layover_inland': d.get('layover_inland'),
                    'starts_at_homebase': bool(d.get('starts_at_homebase')),
                    'ends_at_homebase': bool(d.get('ends_at_homebase')),
                    'requires_commute': bool(d.get('requires_commute')),
                    'is_workday': bool(d.get('is_workday')),
                    'start_time': d.get('start_time', '') or '',
                    'end_time': d.get('end_time', '') or '',
                    'duty_duration_minutes': int(d.get('duty_duration_minutes') or 0),
                    'confidence': float(d.get('confidence') or 0),
                    'raw_lines': list(d.get('raw_lines') or []),
                },
                'classifier_result': {
                    'klass': 'Frei',
                    'amount': 0.0,
                    'reason': at,
                    'bmf_land': '',
                    'bmf_tagtyp': '',
                    'counted_as_workday': False,
                    'counted_as_fahrtag': False,
                    'counted_as_hotel_nacht': False,
                },
                'sources': ['DP'],
                'diagnostics': {
                    'reader_warning': '',
                    'classifier_warning': '',
                    'bmf_mapping_issue': '',
                    'unresolved_reason': '',
                },
            })
            continue

        if at == 'standby':
            klass = 'Standby'
            reason = 'Standby zuhause — AT, kein FT, kein VMA'

        elif at == 'office':
            # v8.20.0/v8.20.1: Office mit total>=480min + kein Hotel + nicht in
            # Cluster + keine aktive Auslands-SE → Z72 (Inland-Tagestrip >8h).
            # total nutzt _total_minutes_for_z72 (Dienst-Zeit + 2×commute ODER
            # Tour-Abwesenheits-Zeit direkt).
            total_min_o, duty_known_o, time_src_o = _total_minutes_for_z72(d, commute_minutes)
            in_cluster_o = i in cluster_for_idx
            has_active_foreign_se_o = (
                se.get('count', 0) > 0
                and se.get('stfrei_inland') is False
                and bool(se.get('stfrei_ort'))
            )
            if (not overnight and not prev_overnight and not in_cluster_o
                and not has_active_foreign_se_o
                and duty_known_o and total_min_o >= SAME_DAY_Z72_TOTAL_MINUTES):
                klass = 'Z72'
                eur_added = INLAND_TAGESTRIP_8H
                reason = f'Office Inland >8h (total {total_min_o}min, {time_src_o}) → Z72 14€'
                print(f"[v8-z72-office] datum={datum} total={total_min_o}min src={time_src_o} → Z72")
            else:
                klass = 'Office'
                reason = 'Office am Homebase — AT + FT'

        elif at == 'training':
            if overnight and prev_at != 'training':
                klass = 'Z73'
                reason = 'Schulung mit Hotel — Anreise'
                eur_added = INLAND_AN_ABREISE
            elif (not overnight) and prev_at == 'training' and prev_overnight:
                klass = 'Z73'
                reason = 'Schulung mit Hotel — Abreise'
                eur_added = INLAND_AN_ABREISE
            elif overnight and prev_at == 'training':
                klass = 'Z74' if True else 'Office'  # Volltag in Inland-Schulung mit Hotel = 24h Inland
                reason = 'Schulung mit Hotel — Volltag (Z74 24h)'
                eur_added = INLAND_VOLL_24H
            else:
                # v8.20.0/v8.20.1: Schulung ohne Übernachtung mit total>=480min → Z72.
                # Schulung ohne Zeitinfo oder <8h bleibt Office (kein blind Z72).
                total_min_t, duty_known_t, time_src_t = _total_minutes_for_z72(d, commute_minutes)
                in_cluster_t = i in cluster_for_idx
                has_active_foreign_se_t = (
                    se.get('count', 0) > 0
                    and se.get('stfrei_inland') is False
                    and bool(se.get('stfrei_ort'))
                )
                if (not in_cluster_t and not has_active_foreign_se_t
                    and duty_known_t and total_min_t >= SAME_DAY_Z72_TOTAL_MINUTES):
                    klass = 'Z72'
                    eur_added = INLAND_TAGESTRIP_8H
                    reason = f'Schulung Inland >8h (total {total_min_t}min, {time_src_t}) → Z72 14€'
                    print(f"[v8-z72-training] datum={datum} total={total_min_t}min src={time_src_t} → Z72")
                else:
                    klass = 'Office'
                    reason = 'Schulung am Homebase'

        elif at == 'same_day':
            # Z72-Hard-Gate: kein FL, kein overnight, kein prev_overnight, nicht in Cluster
            in_cluster = i in cluster_for_idx
            cluster_today = cluster_for_idx.get(i)
            if has_fl or overnight:
                klass = 'Issue'
                reason = 'Same-Day verletzt Hard-Gate (FL oder overnight)'
                unresolved_reason = f'same_day mit has_fl={has_fl} overnight={overnight}'
            elif prev_overnight:
                # v8.15: Same-Day mit prev_overnight + aktive Auslands-SE → Z76
                # (Sonnet-Stochastik: Vortag wird mal als overnight=True, mal als
                # False gelesen. Aktive Auslands-SE-Zeile am Same-Day-Tag ist
                # eindeutig Auslandstrip → Z76 BMF-Pauschale).
                if (se.get('count', 0) > 0 and se.get('stfrei_inland') is False
                        and se.get('stfrei_ort')):
                    se_ort_v15 = se.get('stfrei_ort', '')
                    bmf_aus_v15 = _bmf(se_ort_v15)
                    eur_added = float((bmf_aus_v15.get('an_abreise', 0) if bmf_aus_v15 else 28.0) or 0)
                    klass = 'Z76'
                    reason = f'Same-Day Auslandstrip {se_ort_v15} (Z76 >8h, prev_overnight=true Sonnet-Lesefehler)'
                    audit_note = f'{datum}: Same-Day mit Auslands-SE {se_ort_v15} trotz prev_overnight → Z76'
                    print(f"[v8-z76-detail] datum={datum} ort={se_ort_v15} reason='Same-Day Auslandstrip prev_overnight'")
                else:
                    klass = 'Issue'
                    reason = 'Heimkehr aus Vortag-Tour — separater Tour-Abschluss'
                    unresolved_reason = 'same_day nach prev_overnight (Mischfall)'
            elif in_cluster and cluster_today and cluster_today.get('has_foreign'):
                # Same-Day in Auslands-Cluster: Anreise-Tag der Auslandstour
                klass = 'Z76'
                eur_added = 28.0
                reason = 'Same-Day im Auslands-Cluster (Z76 An/Ab)'
                audit_note = f'{datum}: same_day im Auslands-Cluster — als Z76 klassifiziert'
            elif in_cluster and cluster_today and cluster_today.get('has_inland'):
                klass = 'Z73'
                eur_added = INLAND_AN_ABREISE
                reason = 'Same-Day im Inland-Cluster (Z73 An/Ab)'
            elif (se.get('count', 0) > 0 and se.get('stfrei_inland') is False
                  and se.get('stfrei_ort')):
                # v8.8: Same-Day mit AUSLANDS-SE-Stempel (z.B. TLV/CAI/REK) — das ist
                # ein Same-Day-Auslandstrip → Z76 mit BMF-Pauschale für >8h
                # (an_abreise-Satz, weil keine Übernachtung).
                se_ort_fix = se.get('stfrei_ort', '')
                bmf_aus = _bmf(se_ort_fix)
                eur_added = float((bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0) or 0)
                klass = 'Z76'
                reason = f'Same-Day Auslandstrip {se_ort_fix} (Z76 >8h)'
                audit_note = f'{datum}: Same-Day mit Auslands-SE {se_ort_fix} → Z76'
                print(f"[v8-z76-detail] datum={datum} ort={se_ort_fix} bmf_land={(bmf_aus or {}).get('land','?')} tagtyp=an_abreise amount={eur_added:.2f} reason='Same-Day Auslandstrip'")
            else:
                # v8.19.1 / v8.20.1 — strikte Trennung "duty bekannt" vs "duty fehlt":
                #
                # 1. duty bekannt UND total_for_z72 >= 480     → Z72 via duty-Pfad
                # 2. duty bekannt UND total_for_z72 <  480     → ZeroDay (Sonnet-Read
                #    glaubwürdig, KEIN Routing-Override)
                # 3. duty fehlt/None UND Inland-Roundtrip      → Z72 via Routing-Override
                # 4. duty fehlt/None UND kein Inland-Routing   → ZeroDay (kein Indiz)
                #
                # total_for_z72 berücksichtigt time_is_absence-Flag (v8.20.1):
                # - false: duty + 2×commute (Dienst-Zeit, Fahrzeit hinzu)
                # - true:  duty (bereits Tour-Abwesenheits-Zeit, kein doppelter commute)
                total_min, duty_known, time_src = _total_minutes_for_z72(d, commute_minutes)
                routing = d.get('routing') or []
                _hb_up = (homebase or 'FRA').upper()
                is_routing_inland_sameday = (
                    len(routing) >= 3
                    and (routing[0] or '').upper() == _hb_up
                    and (routing[-1] or '').upper() == _hb_up
                    and any(_is_inland_code((r or '').upper()) for r in routing[1:-1])
                )
                if duty_known and total_min >= SAME_DAY_Z72_TOTAL_MINUTES:
                    klass = 'Z72'
                    eur_added = INLAND_TAGESTRIP_8H
                    reason = f'Same-Day Z72 — total={total_min}min ({time_src}) ≥ {SAME_DAY_Z72_TOTAL_MINUTES}min'
                    print(f"[v8-z72-duration] datum={datum} total={total_min}min src={time_src} counted=Z72")
                elif duty_known:
                    klass = 'ZeroDay'
                    reason = f'Same-Day < 8h (total={total_min}min, {time_src}) — kein VMA'
                    print(f"[v8-z72-duration] datum={datum} total={total_min}min src={time_src} counted=ZeroDay")
                elif is_routing_inland_sameday:
                    klass = 'Z72'
                    eur_added = INLAND_TAGESTRIP_8H
                    routing_str = '-'.join(routing)
                    reason = f'Same-Day Inland-Tagestrip Routing {routing_str} (duty fehlt — Routing als Plausi-Quelle) → Z72 14€'
                    print(f"[v8-z72-routing] datum={datum} routing={routing_str} duty=None → Z72 (Routing-Override)")
                else:
                    klass = 'ZeroDay'
                    reason = 'Same-Day ohne duty-Info und ohne Inland-Routing — kein VMA-Indiz'
                    print(f"[v8-z72-duration] datum={datum} duty=None routing={routing} counted=ZeroDay")

        elif at == 'tour':
            cluster = cluster_for_idx.get(i)
            cluster_foreign = bool(cluster and cluster.get('has_foreign'))
            cluster_inland = bool(cluster and cluster.get('has_inland'))
            cluster_mixed = cluster_foreign and cluster_inland
            is_anreise = bool(cluster and cluster.get('indices') and i == cluster['indices'][0])
            is_abreise = bool(cluster and cluster.get('indices') and i == cluster['indices'][-1])

            today_layover_ort = se.get('stfrei_ort') or d.get('layover_ort', '') or ''
            today_layover_inland = None
            if overnight and today_layover_ort:
                today_layover_inland = _is_inland_code(today_layover_ort)
            yesterday_layover_ort = ''
            yesterday_layover_inland = None
            if prev and prev_overnight:
                yesterday_layover_ort = prev['se'].get('stfrei_ort') or prev['dp'].get('layover_ort', '') or ''
                if yesterday_layover_ort:
                    yesterday_layover_inland = _is_inland_code(yesterday_layover_ort)

            classified = False

            # ─── Fall A: Heute Übernachtung ───
            if overnight and today_layover_inland is False:
                # v8.10: Auslandstour-Anreise mit Abend-Briefing (start_time >= 18:00)
                # ist hauptsächlich in Deutschland verbracht (User in DE bis Briefing,
                # dann lange Flugnacht). Steuerlich Z73 Inland-Anreise 14€, nicht
                # Z76 BMF-An/Ab des Ziellands.
                # Die Mittel-Tage und der Heimkehrtag bleiben Z76 wie gewohnt.
                start_time_str = d.get('start_time', '') or ''
                evening_anreise = False
                if is_anreise and start_time_str and ':' in start_time_str:
                    try:
                        start_h = int(start_time_str.split(':')[0])
                        if start_h >= EVENING_FOREIGN_TOUR_START_HOUR:
                            evening_anreise = True
                    except (ValueError, IndexError):
                        pass

                if evening_anreise:
                    klass = 'Z73'
                    z73_type = 'evening_foreign_tour_start'
                    eur_added = INLAND_AN_ABREISE
                    reason = f'Auslandstour-Anreise mit Abend-Briefing {start_time_str} (>= {EVENING_FOREIGN_TOUR_START_HOUR}:00) → Inland-Anreise 14€'
                    audit_note = f'{datum}: Auslandstour-Anreise nach {today_layover_ort}, Briefing {start_time_str} → Z73 Inland (Tag dominant in DE)'
                    print(f"[v8-z73-detail] datum={datum} ort={today_layover_ort} start={start_time_str} reason='Abend-Anreise → Z73 Inland'")
                else:
                    klass = 'Z76'
                    bmf_aus = _bmf(today_layover_ort)
                    if is_anreise or is_abreise:
                        satz = (bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0)
                    else:
                        satz = (bmf_aus.get('voll_24h', 0) if bmf_aus else 28.0)
                    eur_added = float(satz or 0)
                    reason = f'Auslands-Layover {today_layover_ort} (Z76 {"An/Ab" if (is_anreise or is_abreise) else "Volltag"})'
                classified = True

            elif overnight and today_layover_inland is True:
                is_volltag = not (is_anreise or is_abreise)
                hb_upper = (homebase or 'FRA').upper()
                # v8.5: Wenn der Inland-Layover-Ort die Homebase ist UND der
                # Cluster Auslandscluster ist → das ist ein Homebase-Stempel
                # auf einem Auslandstour-Tag, kein echter Inland-Layover.
                # → Z76 (mit Cluster-Ziel-Land), nicht Z73/Z74.
                # Inland-Layover-Ort ≠ Homebase (z.B. MUC bei FRA-Crew, Mixed-Tour)
                # = echter Inland-Layover → Z73/Z74 wie gewohnt.
                inland_is_homebase = today_layover_ort.upper() == hb_upper
                if cluster_foreign and inland_is_homebase:
                    # v8.12: Vor der Z76-Standard-Logik: Abend-Anreise-Regel auch hier.
                    # Bei FRA-SE-Stempel + cluster_foreign + is_anreise + start_time>=18
                    # → Z73 Inland-Anreise 14€ (statt Z76 An/Ab des Ziellands).
                    # Tag dominant in DE verbracht (User boardet abends in FRA).
                    start_time_str_v12 = d.get('start_time', '') or ''
                    evening_anreise_v12 = False
                    if is_anreise and start_time_str_v12 and ':' in start_time_str_v12:
                        try:
                            sh = int(start_time_str_v12.split(':')[0])
                            if sh >= EVENING_FOREIGN_TOUR_START_HOUR:
                                evening_anreise_v12 = True
                        except (ValueError, IndexError):
                            pass

                    if evening_anreise_v12:
                        klass = 'Z73'
                        z73_type = 'evening_foreign_tour_start'
                        eur_added = INLAND_AN_ABREISE
                        reason = f'Auslandstour-Anreise mit FRA-Stempel + Abend-Briefing {start_time_str_v12} → Inland 14€'
                        audit_note = f'{datum}: Homebase-SE-Stempel {today_layover_ort}, Briefing {start_time_str_v12} → Z73 Inland (Tag dominant in DE)'
                        print(f"[v8-z73-detail] datum={datum} stempel={today_layover_ort} start={start_time_str_v12} reason='FRA-Stempel + Abend-Anreise → Z73'")
                    else:
                        target_iata = ''
                        for ci in cluster['indices']:
                            cm = sorted_days[ci]
                            if cm['dp'].get('overnight_after_day'):
                                cand = cm['se'].get('stfrei_ort') or cm['dp'].get('layover_ort', '')
                                if cand and not _is_inland_code(cand):
                                    target_iata = cand
                                    break
                        klass = 'Z76'
                        bmf_aus = _bmf(target_iata)
                        if is_anreise or is_abreise:
                            eur_added = float((bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0) or 0)
                            position = 'An/Ab'
                        else:
                            eur_added = float((bmf_aus.get('voll_24h', 0) if bmf_aus else 28.0) or 0)
                            position = 'Volltag'
                        reason = f'Auslandstour-{position} (Homebase-Stempel {today_layover_ort}, Ziel {target_iata or "?"}) Z76'
                        audit_note = f'{datum}: Homebase-SE-Stempel {today_layover_ort} bei Auslands-Cluster — als Z76 {position}'
                        print(f"[v8-inland-blocked-foreign-cluster] datum={datum} stempel={today_layover_ort} reason='Homebase-Stempel auf Auslandstour — kein Z73/Z74'")
                elif is_volltag:
                    # Echter Inland-Volltag (Inland-Layover ≠ Homebase, oder reiner Inland-Cluster)
                    klass = 'Z74'
                    eur_added = INLAND_VOLL_24H
                    reason = f'Inland-Mittel-Tag {today_layover_ort} (Z74 24h)'
                else:
                    klass = 'Z73'
                    eur_added = INLAND_AN_ABREISE
                    reason = f'Inland-Layover {today_layover_ort} (Z73 An/Ab{"" if not cluster_mixed else " im Mixed"})'
                classified = True

            elif overnight and today_layover_inland is None:
                if cluster_foreign and not cluster_inland:
                    klass = 'Z76'
                    eur_added = 28.0
                    reason = 'Tour-Übernachtung ohne Ort (Cluster=Ausland)'
                    audit_note = f'{datum}: overnight ohne Layover-Ort, Cluster Ausland → Z76 28€'
                elif cluster_inland and not cluster_foreign:
                    if is_anreise or is_abreise:
                        klass = 'Z73'
                        eur_added = INLAND_AN_ABREISE
                    else:
                        klass = 'Z74'
                        eur_added = INLAND_VOLL_24H
                    reason = 'Tour-Übernachtung ohne Ort (Cluster=Inland)'
                    audit_note = f'{datum}: overnight ohne Ort, Cluster Inland → konservativ'
                else:
                    klass = 'Issue'
                    reason = 'Tour-Übernachtung ohne Ort, kein Cluster-Kontext'
                    unresolved_reason = 'overnight=true, kein Layover-Ort, kein Cluster-Kontext'
                classified = True

            # ─── Fall B: Heute KEINE Übernachtung — Heimkehr ───
            elif (not overnight) and prev_overnight:
                # v8.5: prev_cluster prüfen — Homebase-Stempel im Vortag bei
                # Auslandscluster ist Auslands-Heimkehr (kein Z73 Inland).
                cluster_prev = cluster_for_idx.get(i - 1) if i > 0 else None
                prev_cluster_foreign = bool(cluster_prev and cluster_prev.get('has_foreign'))
                hb_upper = (homebase or 'FRA').upper()
                yesterday_is_homebase = yesterday_layover_ort.upper() == hb_upper
                if yesterday_layover_inland is False:
                    klass = 'Z76'
                    bmf_aus = _bmf(yesterday_layover_ort)
                    eur_added = float((bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0) or 0)
                    reason = f'Auslands-Heimkehr (Vortag {yesterday_layover_ort}) Z76'
                elif yesterday_layover_inland is True and prev_cluster_foreign and yesterday_is_homebase:
                    # Homebase-Stempel im Vortag bei Auslandscluster = Heimkehr aus Auslandstour
                    target_iata = ''
                    for ci in cluster_prev.get('indices', []):
                        cm = sorted_days[ci]
                        if cm['dp'].get('overnight_after_day'):
                            cand = cm['se'].get('stfrei_ort') or cm['dp'].get('layover_ort', '')
                            if cand and not _is_inland_code(cand):
                                target_iata = cand
                                break
                    klass = 'Z76'
                    bmf_aus = _bmf(target_iata)
                    eur_added = float((bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0) or 0)
                    reason = f'Auslands-Heimkehr (Homebase-Stempel {yesterday_layover_ort}, Ziel {target_iata or "?"}) Z76'
                    audit_note = f'{datum}: Heimkehr aus Auslandscluster mit Homebase-Stempel — als Z76'
                    print(f"[v8-inland-blocked-foreign-cluster] datum={datum} stempel={yesterday_layover_ort} reason='Heimkehr aus Auslandscluster — kein Z73'")
                elif yesterday_layover_inland is True:
                    klass = 'Z73'
                    eur_added = INLAND_AN_ABREISE
                    reason = f'Inland-Heimkehr (Vortag {yesterday_layover_ort}) Z73'
                else:
                    # Vortag-Layover-Ort unbekannt — Cluster-Kontext nutzen
                    cluster_prev = cluster_for_idx.get(i - 1) if i > 0 else None
                    if cluster_prev and cluster_prev.get('has_foreign'):
                        klass = 'Z76'
                        eur_added = 28.0
                        reason = 'Heimkehr aus Auslands-Cluster (Vortag-Ort unklar)'
                        audit_note = f'{datum}: Heimkehr ohne Vortag-Ort, Cluster Ausland → Z76 28€'
                    else:
                        klass = 'Issue'
                        reason = 'Heimkehr — Vortag-Layover unklar'
                        unresolved_reason = 'Heimkehr ohne Vortag-Layover-Ort und ohne Cluster-Kontext'
                classified = True

            # ─── Fall C: kein overnight, kein prev_overnight ───
            elif (not overnight) and (not prev_overnight):
                if cluster_foreign:
                    klass = 'Z76'
                    eur_added = 28.0
                    reason = 'Tour-Tag im Auslands-Cluster (An/Stop)'
                    audit_note = f'{datum}: tour ohne overnight im Auslands-Cluster → Z76 28€'
                elif cluster_inland:
                    klass = 'Z73'
                    eur_added = INLAND_AN_ABREISE
                    reason = 'Tour-Tag im Inland-Cluster (An/Stop)'
                else:
                    # ECHT isoliert — Same-Day-Heuristik
                    k, e, r, a, u = _resolve_isolated_tour_day(i, m, d, se)
                    klass, eur_added, reason = k, e, r
                    if a:
                        audit_note = a
                    if u:
                        unresolved_reason = u
                    print(f"[v8-daytrip-resolved] datum={datum} klass={klass} eur={eur_added:.2f} reason='{reason[:80]}'")
                classified = True

            if not classified:
                klass = 'Issue'
                reason = 'tour-Tag konnte nicht klassifiziert werden'
                unresolved_reason = 'tour ohne klare Klassifikations-Spur'

            cluster_id = cluster['_id'] if cluster else -1
            print(f"[v8-classify-day] datum={datum} cluster={cluster_id} ort={today_layover_ort or '-'} klass={klass} reason='{reason[:80]}'")

        else:
            # at='unknown'/'none' und nicht in Cluster — aktive SE → Re-Klassifikation
            has_active_se = se.get('count', 0) > 0 and float(se.get('stfrei_total', 0) or 0) > 0
            if has_active_se:
                se_ort = se.get('stfrei_ort', '')
                se_inland = se.get('stfrei_inland')
                if se_inland is False:
                    bmf_aus = _bmf(se_ort)
                    satz = float((bmf_aus.get('an_abreise', 0) if bmf_aus else 28.0) or 0)
                    klass = 'Z76'
                    eur_added = satz
                    reason = f'Aktive Auslands-SE {se_ort} (DP={at}) Z76'
                    audit_note = f'{datum}: DP={at} mit aktiver Auslands-SE {se_ort} → Z76'
                elif se_inland is True:
                    klass = 'Z73'
                    eur_added = INLAND_AN_ABREISE
                    reason = f'Aktive Inland-SE {se_ort} (DP={at}) Z73'
                    audit_note = f'{datum}: DP={at} mit aktiver Inland-SE {se_ort} → Z73'
                else:
                    klass = 'Issue'
                    reason = f'Aktive SE {se_ort} aber Inland/Ausland unklar'
                    unresolved_reason = f'aktive SE-Zeile ohne stfrei_inland-Klarheit (DP={at})'
            else:
                # v8.3: at='unknown'/'none' ohne SE-Spur → KEIN Arbeitstag.
                # Diese Tage sind weder Frei (nicht eindeutig) noch Dienst.
                # Wir markieren sie als ZeroDay aber NICHT-dienstlich, sodass
                # arbeitstage sie nicht zählt.
                klass = 'ZeroDay' if at in ('unknown', 'none', '') else 'Issue'
                reason = f'Activity-Type {at} ohne SE/Cluster-Spur — nicht als Arbeitstag gewertet'
                # zero_day_dienstlich = False: standalone unknown ist kein Dienst
                zero_day_dienstlich = False  # noqa: F841 (später ausgewertet)
                if klass == 'Issue':
                    unresolved_reason = f'activity_type={at} unklar'

        # v8.18.1: Anti-Stochastik-Rescue mit verschärften Bedingungen.
        # Issue → Z76 NUR wenn ALLE Bedingungen erfüllt:
        # 1. klass='Issue' (Klassifikator hat keine eindeutige Klasse gefunden)
        # 2. Aktive (nicht-storno) SE-Zeile mit stfrei_total > 0
        # 3. SE-Ort ist Auslands-IATA (stfrei_inland=False)
        # 4. BMF-Mapping existiert für SE-Ort (kein 28€-Pauschal-Fallback)
        # 5. Tag ist nicht aus Frei/Urlaub/Krank-Pfad gekommen (würde mit
        #    'continue' den Loop verlassen — Issue-klass kommt nur aus echten
        #    Klassifikations-Pfaden, nie aus Frei-Pfad)
        # Wenn BMF-Mapping fehlt: Issue bleibt + iata_unknown wird sichtbar.
        has_active_se_final = se.get('count', 0) > 0 and float(se.get('stfrei_total', 0) or 0) > 0
        if (klass == 'Issue' and has_active_se_final
                and se.get('stfrei_inland') is False and se.get('stfrei_ort')):
            se_ort_rescue = se.get('stfrei_ort', '')
            se_betrag_rescue = float(se.get('stfrei_total', 0) or 0)
            bmf_aus_rescue = _bmf(se_ort_rescue)  # tracked iata_unknown / bmf_missing
            if bmf_aus_rescue and bmf_aus_rescue.get('an_abreise'):
                # Rescue legitim: BMF-Mapping vorhanden
                eur_added = float(bmf_aus_rescue.get('an_abreise', 0) or 0)
                old_reason_rescue = reason
                klass = 'Z76'
                reason = f'Aktive Auslands-SE {se_ort_rescue} → Z76-Rescue (war: {old_reason_rescue[:60]})'
                audit_note = f'{datum}: aktive Auslands-SE {se_ort_rescue} → Z76 (Issue→Z76-Rescue)'
                unresolved_reason = None
                # v8.18.1: strukturierter Rescue-Audit-Eintrag
                from bmf_data import IATA_TO_BMF
                rescues.append({
                    'datum':         datum,
                    'rescue_type':   'active_foreign_se_issue_to_z76',
                    'rescue_reason': old_reason_rescue,
                    'se_ort':        se_ort_rescue,
                    'se_betrag':     se_betrag_rescue,
                    'bmf_land':      IATA_TO_BMF.get(se_ort_rescue.upper(), '') or '',
                    'bmf_tagtyp':    'an_abreise',
                    'amount':        eur_added,
                    'original_klass':'Issue',
                })
                print(f"[v8-anti-stochastik] datum={datum} ort={se_ort_rescue} betrag={se_betrag_rescue:.2f} reason='Issue→Z76 (BMF-Mapping vorhanden)'")
            else:
                # Kein BMF-Mapping → Rescue NICHT durchführen, Issue bleibt
                # Plus _bmf() hat schon iata_unknown/bmf_missing geloggt
                print(f"[v8-anti-stochastik-skip] datum={datum} ort={se_ort_rescue} reason='Issue bleibt — BMF-Mapping fehlt'")

        # VMA-Unmapped-SE-Check: aktive SE ohne Z72/73/74/76?
        if has_active_se_final and klass not in ('Z72', 'Z73', 'Z74', 'Z76'):
            vma_unmapped_se.append({
                'datum': datum,
                'stfrei_ort': se.get('stfrei_ort', ''),
                'stfrei_total': se.get('stfrei_total', 0),
                'zwoelftel': se.get('zwoelftel', 0),
                'klass': klass,
                'reason': reason,
                'lines_count': se.get('count', 0),
            })
            print(f"[v8-vma-unmapped-se] datum={datum} ort={se.get('stfrei_ort','')} "
                  f"betrag={se.get('stfrei_total',0):.2f} klass={klass} reason='{reason[:60]}'")
            unresolved_reason = unresolved_reason or f'aktive SE-Zeile ohne VMA-Klassifikation (klass={klass})'

        if audit_note:
            audit_notes.append(audit_note)
        if unresolved_reason:
            unresolved_days.append(f'{datum}: {unresolved_reason}')
            print(f"[v8-unresolved-day] datum={datum} marker={d.get('raw_marker','') or at} "
                  f"routing={'-'.join(d.get('routing') or [])} se_count={se.get('count',0)} "
                  f"se_ort={se.get('stfrei_ort','')} reason='{unresolved_reason}'")

        # v8.3: dienstlich-Flag bestimmen — entscheidet ob ZeroDay/Issue als
        # Arbeitstag zählt. Z72/Z73/Z74/Z76/Office/Standby/Training sind immer
        # dienstlich. ZeroDay ist nur dienstlich wenn aus echtem Tour/Same-Day
        # (= DP hatte echten Dienst-Marker). Unknown/none ohne Spur → False.
        if klass in ('Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Standby'):
            dienstlich = True
        elif klass == 'ZeroDay':
            # Original-AT vor in_extended_cluster-Patch: aus DP holen
            orig_at = d.get('activity_type', 'unknown')
            dienstlich = orig_at in ('tour', 'same_day', 'office', 'training', 'standby')
        else:
            dienstlich = False  # Frei/Issue

        # v8.7/v8.18: BMF-Land/Tagtyp für Z76 ableiten.
        # v8.18: Z76 MUSS einen tagtyp haben — leerer tagtyp = fallback_issue
        # mit Audit-Hinweis (statt still leer zu bleiben).
        bmf_land = ''
        bmf_key = ''
        bmf_tagtyp = ''
        if klass == 'Z76':
            try:
                from bmf_data import IATA_TO_BMF
                # Primary: heutiger SE-Ort oder DP-Layover
                ort_for_bmf = (se.get('stfrei_ort') or d.get('layover_ort') or '').upper()
                # v8.19.0 Fix 2a: Wenn ort Inland (z.B. FRA-Stempel auf Auslandstour-Anreise)
                # → routing-tail probieren (Ziel-Flughafen)
                if ort_for_bmf and _is_inland_code(ort_for_bmf):
                    routing = d.get('routing') or []
                    if routing:
                        tail = (routing[-1] or '').upper()
                        if tail and not _is_inland_code(tail):
                            ort_for_bmf = tail
                # v8.19.0 Fix 2b: Wenn ort leer oder Inland (z.B. Heimkehrtag)
                # → Vortag-Layover-Ort verwenden
                if not ort_for_bmf or _is_inland_code(ort_for_bmf):
                    if prev:
                        prev_layover = (prev['se'].get('stfrei_ort')
                                        or prev['dp'].get('layover_ort','') or '').upper()
                        if prev_layover and not _is_inland_code(prev_layover):
                            ort_for_bmf = prev_layover
                if ort_for_bmf and not _is_inland_code(ort_for_bmf):
                    bmf_land = IATA_TO_BMF.get(ort_for_bmf, '') or ''
                    bmf_key = bmf_land
            except Exception:
                pass
            # tagtyp aus Reason ableiten (Bestpraxis — wir setzen reason-Strings konsistent)
            r_low = (reason or '').lower()
            if 'same-day' in r_low or 'same_day' in r_low or '>8h' in r_low:
                bmf_tagtyp = 'same_day_8h'
            elif 'volltag' in r_low or '24h' in r_low:
                bmf_tagtyp = 'voll_24h'
            elif 'heimkehr' in r_low or 'abreise' in r_low or 'an/ab' in r_low:
                bmf_tagtyp = 'an_abreise'
            elif 'anreise' in r_low:
                bmf_tagtyp = 'anreise'
            else:
                # v8.18: Fallback — Z76 ohne ableitbaren Tagtyp ist suspect
                bmf_tagtyp = 'fallback_issue'
                audit_note = audit_note or f'{datum}: Z76 ohne ableitbaren bmf_tagtyp (Pauschal-Fallback)'

        # v8.7: Architektur-saubere nested Audit-Struktur — getrennt nach
        # Reader-Fakt vs Classifier-Entscheidung vs Diagnostics. Backward-
        # Compat: flache Felder (klass, begruendung, marker, routing, eur,
        # dienstlich) bleiben für PDF und alte Konsumenten.
        reader_facts = {
            'datum': datum,
            'activity_type': d.get('activity_type', ''),
            'marker_raw': d.get('raw_marker', ''),
            'routing': list(d.get('routing') or []),
            'has_fl': bool(d.get('has_fl')),
            'overnight_after_day': bool(d.get('overnight_after_day')),
            'layover_ort': d.get('layover_ort', '') or '',
            'layover_inland': d.get('layover_inland'),
            'starts_at_homebase': bool(d.get('starts_at_homebase')),
            'ends_at_homebase': bool(d.get('ends_at_homebase')),
            'requires_commute': bool(d.get('requires_commute')),
            'is_workday': bool(d.get('is_workday')),
            'start_time': d.get('start_time', '') or '',
            'end_time': d.get('end_time', '') or '',
            'duty_duration_minutes': int(d.get('duty_duration_minutes') or 0),
            'confidence': float(d.get('confidence') or 0),
            'raw_lines': list(d.get('raw_lines') or []),
        }
        # v8.9: effective_ort-Tracking für Diagnose vs Classifier-Konsistenz
        dp_layover_ort_v = (d.get('layover_ort') or '').upper()
        se_effective_ort = (se.get('stfrei_ort') or '').upper()
        # Classifier-Priorität: SE-Ort vor DP-layover_ort
        classifier_effective_ort = se_effective_ort or dp_layover_ort_v

        # v8.16: Hotel-Counter mit harten Bedingungen — synchron zum
        # zentralen _hotel_check (oben). Hier nur die statische Logik
        # ohne prev_m-Zugriff (das macht der Counter selbst).
        is_evening_anreise_z73 = (klass == 'Z73' and z73_type == 'evening_foreign_tour_start')
        hotel_layover_ort = ((d.get('layover_ort') or se.get('stfrei_ort') or '').upper())
        hotel_homebase = (homebase or 'FRA').upper()
        has_active_se_for_hotel = se.get('count', 0) > 0 and float(se.get('stfrei_total', 0) or 0) > 0
        counted_hotel = (
            bool(d.get('overnight_after_day'))
            and klass in ('Z73', 'Z74', 'Z76')
            and not is_evening_anreise_z73
            and bool(hotel_layover_ort)
            and hotel_layover_ort != hotel_homebase
            and not (at in ('unknown', 'none', '') and not has_active_se_for_hotel)
        )

        # v8.17: counted_as_reinigungstag — getrennt vom counted_as_workday.
        # Reinigungskosten zählen nur Tage mit Uniform-/Dienstkleidungs-Bezug.
        # Standby/Rufbereitschaft zuhause zählen NICHT (kein Uniform-Tag).
        # Mehrtages-Training-Block-Folgetage zählen NICHT (gleiche Logik wie Fahrtage).
        # Frei/Issue/SE-only zählen NICHT.
        REINIGUNG_KLASSEN = ('Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Training')
        in_training_skip = i in training_seq_skip
        counted_reinigung = (
            klass in REINIGUNG_KLASSEN
            and not in_training_skip
            and not is_evening_anreise_z73  # Tag in DE im Flugzeug — kein Uniform-Tag
        ) or (
            # ZeroDay nur wenn explizit dienstlich UND Same-Day-Pattern (echter Tour-Tag)
            klass == 'ZeroDay' and dienstlich
        )

        classifier_result = {
            'klass': klass,
            'amount': round(eur_added, 2),
            'reason': reason,
            'bmf_land': bmf_land,
            'bmf_key': bmf_key,
            'bmf_tagtyp': bmf_tagtyp,
            'z73_type': z73_type,           # v8.14: explizites Subtyp-Flag
            'counted_as_workday': klass in ('Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Standby') or (klass == 'ZeroDay' and dienstlich),
            'counted_as_fahrtag': bool(d.get('requires_commute')) and klass not in ('Frei', 'Issue', 'Standby'),
            'counted_as_hotel_nacht': counted_hotel,
            'counted_as_reinigungstag': counted_reinigung,
            'is_vma_correction_only': is_evening_anreise_z73,
            # v8.9: effective_ort sichtbar machen — bei Bug sofort sehen welche Quelle Classifier nutzt
            'dp_layover_ort':           dp_layover_ort_v,
            'se_effective_ort':         se_effective_ort,
            'classifier_effective_ort': classifier_effective_ort,
        }
        sources = []
        if d.get('activity_type'): sources.append('DP')
        if se.get('count', 0) > 0: sources.append('SE')
        if klass == 'Z76' and bmf_land: sources.append('BMF2025')
        diagnostics = {
            'reader_warning': d.get('notes', '') or '',
            'classifier_warning': audit_note or '',
            'bmf_mapping_issue': '' if (klass != 'Z76' or bmf_land)
                                  else f'BMF-Mapping fehlt für {d.get("layover_ort","")}',
            'unresolved_reason': unresolved_reason or '',
        }

        tage_detail.append({
            # Backward-Compat (PDF + alte Konsumenten):
            'datum': datum,
            'klass': klass,
            'begruendung': reason,
            'marker': d.get('raw_marker', '') or at,
            'routing': '-'.join(d.get('routing') or []),
            'tour_dauer': 1,
            'eur': round(eur_added, 2),
            'dienstlich': dienstlich,
            # v8.7: Nested Audit-Struktur (Architektur-Trennung sichtbar):
            'reader_facts':      reader_facts,
            'classifier_result': classifier_result,
            'sources':           sources,
            'diagnostics':       diagnostics,
        })

    # ── Counter aus klass aggregieren ──
    z72_tage = sum(1 for t in tage_detail if t['klass'] == 'Z72')
    z73_tage = sum(1 for t in tage_detail if t['klass'] == 'Z73')
    z74_tage = sum(1 for t in tage_detail if t['klass'] == 'Z74')
    z76_tage = sum(1 for t in tage_detail if t['klass'] == 'Z76')
    office_tage = sum(1 for t in tage_detail if t['klass'] == 'Office')
    standby_tage = sum(1 for t in tage_detail if t['klass'] == 'Standby')
    zero_tage = sum(1 for t in tage_detail if t['klass'] == 'ZeroDay')
    sonstige_tage = sum(1 for t in tage_detail if t['klass'] in ('Issue', 'Sonstiges'))
    issue_tage = sum(1 for t in tage_detail if t['klass'] == 'Issue')

    z72_eur = round(sum(t['eur'] for t in tage_detail if t['klass'] == 'Z72'), 2)
    z73_eur = round(sum(t['eur'] for t in tage_detail if t['klass'] == 'Z73'), 2)
    z74_eur = round(sum(t['eur'] for t in tage_detail if t['klass'] == 'Z74'), 2)
    z76_eur = round(sum(t['eur'] for t in tage_detail if t['klass'] == 'Z76'), 2)

    # v8.18.3: Single source of truth ist counted_as_workday-Flag in
    # classifier_result (gesetzt in der Klassifikations-Schleife oben).
    # Logik dort: klass in {Z72,Z73,Z74,Z76,Office,Standby} oder
    # (klass==ZeroDay und dienstlich). HARD_AT_KLASSEN nur noch
    # für Counter-Print unten benötigt.
    HARD_AT_KLASSEN = {'Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Standby'}
    arbeitstage = sum(
        1 for t in tage_detail
        if (t.get('classifier_result') or {}).get('counted_as_workday')
    )
    # v8.17: reinigungstage = nur Tage mit Uniform-/Dienstkleidungs-Bezug.
    # Aus classifier_result.counted_as_reinigungstag aggregiert.
    reinigungstage = sum(
        1 for t in tage_detail
        if (t.get('classifier_result') or {}).get('counted_as_reinigungstag')
    )
    zero_dienstlich_tage = sum(1 for t in tage_detail if t['klass'] == 'ZeroDay' and t.get('dienstlich'))
    zero_skipped_tage    = sum(1 for t in tage_detail if t['klass'] == 'ZeroDay' and not t.get('dienstlich'))
    print(f"[v8-counts-detail] arbeitstage={arbeitstage} reinigungstage={reinigungstage} "
          f"Z72={z72_tage} Z73={z73_tage} Z74={z74_tage} "
          f"Z76={z76_tage} Office={office_tage} Standby={standby_tage} "
          f"ZeroDay_dienstlich={zero_dienstlich_tage} skipped_unknown={zero_skipped_tage} "
          f"Issue={issue_tage}")

    # Fahrtage v8.3: ausschließlich dp.requires_commute (Sonnet oder Heuristik).
    # Kein automatischer Heimkehr-Fahrtag mehr.
    # v8.17: training_seq_skip wird oben (vorm Klassifikations-Loop) berechnet,
    # damit auch counted_as_reinigungstag im classifier_result darauf reagieren kann.

    # v8.18.4: Audit-Notes pro Mehrtages-Sequenz, abhängig von sequence_type
    for seq in training_seq_audit:
        marker_str = ('/' + '/'.join(seq.get('marker_types', []))) if seq.get('marker_types') else ''
        seq_type = seq.get('sequence_type', '')
        if seq.get('why_collapsed'):
            audit_notes.append(
                f"Geschlossener Seminarblock ({seq['start']} bis {seq['end']}, "
                f"{seq['days']} Tage{marker_str}); "
                f"Fahrtag nur am ersten Tag gezählt, "
                f"{seq.get('skipped_fahrtage', 0)} Folgetage übersprungen — "
                f"reiner SM/SEMINAR-Block ohne tägliche Anfahrts-Indizien."
            )
        else:
            audit_notes.append(
                f"Mehrtages-Schulungs-/Office-Sequenz ({seq['start']} bis {seq['end']}, "
                f"{seq['days']} Tage{marker_str}); kein Kollaps, jeder Tag zählt einzeln "
                f"als Fahrtag/Reinigungstag — Grund: {seq_type}."
            )

    # v8.18.3: Fahrtage-Bestimmung schreibt counted_as_fahrtag-Flag
    # zurück in tage_detail[i].classifier_result. Aggregation am Ende
    # liest ausschließlich das Flag.
    fahr_skipped_heimkehr = 0
    fahr_skipped_layover = 0
    fahr_skipped_tourfortsetzung = 0
    fahr_skipped_unknown = 0
    fahr_skipped_standby = 0
    fahr_skipped_training_seq = 0
    fahr_skipped_other = 0
    for i, m in enumerate(sorted_days):
        d = m['dp']
        datum = m['datum']
        prev_d = sorted_days[i-1]['dp'] if i > 0 else None
        klass_today = tage_detail[i]['klass'] if i < len(tage_detail) else 'Issue'
        at = d.get('activity_type', '')
        overnight = bool(d.get('overnight_after_day'))
        prev_overnight = bool(prev_d and prev_d.get('overnight_after_day'))

        # Default-Flag-Zustand für diesen Tag: nicht gezählt
        counted_fahrtag = False

        if klass_today in ('Frei', 'Issue'):
            fahr_skipped_other += 1
        elif at == 'standby':
            fahr_skipped_standby += 1
            print(f"[v8-fahrtage-detail] datum={datum} counted=False reason='Standby zuhause'")
        elif i in training_seq_skip:
            # v8.14: Mehrtages-Training-Sequenz Folgetage — kein eigener Fahrtag
            fahr_skipped_training_seq += 1
            print(f"[v8-fahrtage-detail] datum={datum} counted=False reason='Mehrtägige Trainingssequenz — keine eindeutige tägliche Anfahrt'")
        elif d.get('requires_commute'):
            counted_fahrtag = True
            print(f"[v8-fahrtage-detail] datum={datum} counted=True reason='requires_commute=true ({at})'")
        else:
            if not overnight and prev_overnight:
                fahr_skipped_heimkehr += 1
                reason = 'Heimkehrtag — keine neue Anfahrt'
            elif overnight and prev_overnight:
                fahr_skipped_layover += 1
                reason = 'Layover-Tag (Tourfortsetzung)'
            elif overnight and at == 'tour':
                fahr_skipped_tourfortsetzung += 1
                reason = 'Tour-Layover ohne Homebase-Start'
            elif at in ('unknown', 'none', ''):
                fahr_skipped_unknown += 1
                reason = f'unknown/{at or "leer"}'
            else:
                fahr_skipped_other += 1
                reason = f'no_commute ({at})'
            print(f"[v8-fahrtage-detail] datum={datum} counted=False reason='{reason}'")

        # Flag write-back: Single source of truth ist ab v8.18.3 das Flag
        if i < len(tage_detail):
            cr_writeback = tage_detail[i].setdefault('classifier_result', {})
            cr_writeback['counted_as_fahrtag'] = counted_fahrtag

    fahr_tage = sum(
        1 for t in tage_detail
        if (t.get('classifier_result') or {}).get('counted_as_fahrtag')
    )
    print(f"[v8-fahrtage-summary] counted={fahr_tage} skipped_heimkehr={fahr_skipped_heimkehr} "
          f"skipped_layover={fahr_skipped_layover} skipped_tourfortsetzung={fahr_skipped_tourfortsetzung} "
          f"skipped_unknown={fahr_skipped_unknown} skipped_standby={fahr_skipped_standby} "
          f"skipped_training_seq={fahr_skipped_training_seq} "
          f"skipped_other={fahr_skipped_other}")

    # Hotel-Nächte v8.3: nur Tage mit overnight_after_day=true UND
    # echtem Layover-Ort außerhalb Homebase UND VMA-Klasse Z73/Z74/Z76.
    # Z76-Heimkehrtag (overnight=false) zählt automatisch nicht. Frei/Issue
    # mit overnight=true (DP-Reader-Fehler) wird explizit ausgeschlossen.
    HOTEL_KLASSEN = {'Z73', 'Z74', 'Z76'}
    NON_HOTEL_KLASSEN = {'Frei', 'Issue', 'Standby', 'Office', 'ZeroDay'}
    homebase_upper = (homebase or 'FRA').upper()
    hotel_skipped = []
    hotel_candidate_issues = []  # v8.16: overnight=true ohne layover_ort etc.

    # v8.18.5: Anti-Drift-Helper gegen Sonnet-overnight-Stochastik.
    # Heimkehr zur Homebase schlägt ein eventuell falsch gelesenes overnight=True.

    def _routing_ends_at_homebase(d):
        """Routing-Liste endet an Homebase (letzter IATA)."""
        routing = d.get('routing') or []
        if not routing:
            return False
        last = (routing[-1] or '').upper().strip()
        return last == homebase_upper

    def _ends_at_homebase_robust(d):
        """ends_at_homebase ODER Routing endet an Homebase."""
        return bool(d.get('ends_at_homebase')) or _routing_ends_at_homebase(d)

    def _has_prior_foreign_layover(idx):
        """Hatte der vorhergehende Tag ODER der laufende Tour-Cluster einen
        Layover außerhalb Homebase?"""
        if idx <= 0:
            return False
        prev_d = sorted_days[idx-1]['dp']
        prev_layover = (prev_d.get('layover_ort')
                        or sorted_days[idx-1]['se'].get('stfrei_ort') or '').upper()
        if prev_d.get('overnight_after_day') and prev_layover and prev_layover != homebase_upper:
            return True
        # Cluster-Lookup: gehört Tag zu Cluster, der einen has_foreign-Tag enthält?
        c = cluster_for_idx.get(idx)
        if c and c.get('has_foreign'):
            return True
        return False

    def _has_subsequent_foreign_layover(idx):
        """Gibt es nach idx noch einen Tag mit Layover außerhalb Homebase, BEVOR
        eine neue Homebase-Phase (Frei/Standby/Office) eintritt?"""
        for j in range(idx + 1, len(sorted_days)):
            m_n = sorted_days[j]
            d_n = m_n['dp']
            if not d_n.get('overnight_after_day'):
                # Tag ohne overnight bricht Auslandsphase
                if d_n.get('activity_type') in ('frei', 'urlaub', 'krank',
                                                  'standby', 'office', 'unknown', 'none', ''):
                    return False
                continue
            layover_n = (d_n.get('layover_ort')
                         or m_n['se'].get('stfrei_ort') or '').upper()
            if layover_n and layover_n != homebase_upper:
                return True
            # Layover an Homebase oder leer → weiter schauen
        return False

    def _hotel_check(i, t, m, prev_m):
        """v8.18.5: Harter Check für Hotelnacht.
        counted=True nur wenn:
        - overnight_after_day=true
        - klass in Z73/Z74/Z76
        - z73_type != evening_foreign_tour_start
        - layover_ort vorhanden + ≠ Homebase
        - kein SE-only/unknown-Nachlauf
        - KEIN Heimkehr-Pattern (robust):
            ends_at_homebase ODER Routing endet an Homebase
            UND prior_foreign_layover (Vortag/Cluster)
            UND KEIN nachfolgender Foreign-Layover

        Returns (counted, reason).
        """
        d = m['dp']
        cr = t.get('classifier_result') or {}
        klass = t['klass']
        if not d.get('overnight_after_day'):
            return False, 'no_overnight'
        if klass not in HOTEL_KLASSEN:
            return False, f'klass={klass}_not_hotel'
        if cr.get('z73_type') == 'evening_foreign_tour_start':
            return False, 'evening_foreign_tour_start'
        layover_ort_local = (d.get('layover_ort') or m['se'].get('stfrei_ort') or '').upper()
        if not layover_ort_local:
            return False, 'no_layover_ort'
        if layover_ort_local == homebase_upper:
            return False, f'layover={layover_ort_local}=Homebase'
        at_local = d.get('activity_type', '')
        has_active_se_local = m['se'].get('count', 0) > 0 and float(m['se'].get('stfrei_total', 0) or 0) > 0
        if at_local in ('unknown', 'none', '') and not has_active_se_local:
            return False, 'unknown_without_se_spur'
        # v8.18.5: Robuste Heimkehr-Erkennung — schlägt falsch gelesenes overnight=True
        if _ends_at_homebase_robust(d) and _has_prior_foreign_layover(i):
            if not _has_subsequent_foreign_layover(i):
                return False, 'heimkehr_homebase_kein_neuer_foreign_layover'
        # Legacy-Heimkehr (Vortag overnight + ends_at_homebase) als Fallback
        if prev_m and prev_m['dp'].get('overnight_after_day') and d.get('ends_at_homebase'):
            return False, 'heimkehr_pattern_despite_overnight'
        return True, 'ok'

    # v8.18.3: Hotel-Nächte schreibt counted_as_hotel_nacht-Flag zurück;
    # Aggregat liest ausschließlich das Flag.
    for i, m in enumerate(sorted_days):
        d = m['dp']
        if i >= len(tage_detail):
            continue
        t = tage_detail[i]
        cr_writeback = t.setdefault('classifier_result', {})
        # Default: kein Hotel
        if not d.get('overnight_after_day'):
            cr_writeback['counted_as_hotel_nacht'] = False
            continue
        prev_m = sorted_days[i-1] if i > 0 else None
        klass = t['klass']

        counted, why = _hotel_check(i, t, m, prev_m)
        cr_writeback['counted_as_hotel_nacht'] = bool(counted)

        # extra_hotelnaechte: detaillierte Diagnose (v8.16)
        # Liste enthält JEDEN Tag mit overnight=True PLUS counted/why-Felder,
        # damit der User pro Tag sehen kann warum gezählt/nicht gezählt wurde.
        if not counted:
            hotel_skipped.append(f"{t['datum']}:{why}")
            print(f"[v8-hotel-skipped] datum={t['datum']} reason='{why}'")
            # Bei "no_layover_ort" und klass in Hotel-Klassen → expliziter Audit-Issue
            if why == 'no_layover_ort' and klass in HOTEL_KLASSEN:
                hotel_candidate_issues.append({
                    'datum': t['datum'],
                    'klass': klass,
                    'reason': 'overnight=true aber layover_ort fehlt — Hotel nicht eindeutig'
                })

    hotel_naechte = sum(
        1 for t in tage_detail
        if (t.get('classifier_result') or {}).get('counted_as_hotel_nacht')
    )
    print(f"[v8-hotel-detail] counted={hotel_naechte} skipped={len(hotel_skipped)}")
    print(f"[v8-classify] arbeit={arbeitstage}T fahr={fahr_tage}T hotel={hotel_naechte}T  "
          f"Z72={z72_tage}T/{z72_eur:.2f}€  Z73={z73_tage}T/{z73_eur:.2f}€  "
          f"Z74={z74_tage}T/{z74_eur:.2f}€  Z76={z76_tage}T/{z76_eur:.2f}€  "
          f"audit_notes={len(audit_notes)} unresolved={len(unresolved_days)} "
          f"vma_unmapped={len(vma_unmapped_se)} issues={issue_tage}")

    # ── v8.6: Diagnose-Listen pro Tag ───────────────────────────────
    # Heuristik-basierte "verdächtige" Tag-Listen für Live-Diff-Diagnose.
    # Nicht hard, sondern als Audit-Hilfe für gezielte Verbesserung.
    extra_fahrtage = []          # Tage die Fahrtag sind, aber verdächtig
    extra_arbeitstage = []        # Arbeitstage, deren klass nicht eindeutig Dienst
    extra_hotelnaechte = []       # Hotelnächte mit verdächtigem Kontext
    wrong_z72_candidates = []     # Z72 verdächtig (overnight, FL, prev_overnight)
    missing_z73_candidates = []   # Tag könnte Z73 sein (Inland-Layover ≠ Homebase, aber andere klass)
    missing_z76_candidates = []   # Tag könnte Z76 sein (Auslands-SE ohne Z76, oder cluster_foreign + falsche klass)
    missing_deutschland_14_candidates = []  # Z72/Z73-Inland-Tage die Heuristik vermutet
    # v8.11: Diagnose-Listen für Z76-Diff vs Reference
    aerotax_z76_dates_amounts = []   # alle AeroTAX-Z76-Tage mit Betrag/Land/Tagtyp
    training_commute_candidates = [] # mehrtägige Training-Sequenz (evtl. nicht jeder Tag Fahrtag)
    office_z72_candidates = []       # Office mit duty>=480 ohne klaren Homebase-Bezug
    office_training_time_missing_candidates = []  # v8.21: Office/Schulung ohne Zeitinfo
    unknown_marker_candidates = []  # v8.22 Now-4: unbekannte Kennungen mit raw_marker
    missing_reader_days = []         # Tage in Datum-Range die der DP-Reader weggelassen hat

    hb_upper = (homebase or 'FRA').upper()
    for i, m in enumerate(sorted_days):
        if i >= len(tage_detail):
            continue
        t = tage_detail[i]
        klass = t['klass']
        d = m['dp']
        se = m['se']
        datum = t['datum']
        at = d.get('activity_type', '')
        overnight = bool(d.get('overnight_after_day'))
        prev_d = sorted_days[i-1]['dp'] if i > 0 else None
        prev_overnight = bool(prev_d and prev_d.get('overnight_after_day'))
        cluster = cluster_for_idx.get(i)
        cluster_foreign = bool(cluster and cluster.get('has_foreign'))
        se_ort = se.get('stfrei_ort', '').upper()
        se_inland = se.get('stfrei_inland')
        has_active_se = se.get('count', 0) > 0 and float(se.get('stfrei_total', 0) or 0) > 0
        layover_ort = (d.get('layover_ort') or se_ort).upper()

        # extra_fahrtage: Fahrtag bei verdächtigen Konstellationen
        if d.get('requires_commute') and at not in ('tour', 'same_day', 'office', 'training'):
            extra_fahrtage.append({'datum': datum, 'activity_type': at,
                                   'reason': 'requires_commute=true bei nicht-Dienst-Aktivität'})

        # extra_arbeitstage: AT mit unklarem Klass-Hintergrund
        if klass in ('Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Standby') and at in ('unknown', 'none', 'frei', 'urlaub', 'krank'):
            extra_arbeitstage.append({'datum': datum, 'klass': klass, 'activity_type': at,
                                       'reason': 'AT-Klass auf nicht-dienstlichem DP-Marker'})
        if klass == 'ZeroDay' and t.get('dienstlich') and not has_active_se and at in ('unknown', 'none'):
            extra_arbeitstage.append({'datum': datum, 'klass': klass, 'activity_type': at,
                                       'reason': 'ZeroDay als dienstlich markiert ohne SE-Spur'})

        # v8.16: extra_hotelnaechte erweitert — pro Tag mit overnight=True alle
        # relevanten Felder + counted_as_hotel-Flag + reason. Damit ist der
        # User-Diff-Check direkt möglich.
        if overnight:
            t_diag = tage_detail[i] if i < len(tage_detail) else {}
            cr_diag = t_diag.get('classifier_result') or {}
            counted_h = bool(cr_diag.get('counted_as_hotel_nacht'))
            z73_type_diag = cr_diag.get('z73_type', '') or ''
            why_susp = ''
            if counted_h:
                # Verdächtig wenn gezählt
                if layover_ort in (hb_upper, ''):
                    why_susp = 'Hotel an Homebase oder leerem Ort'
                elif z73_type_diag == 'evening_foreign_tour_start':
                    why_susp = 'evening_foreign_tour_start sollte nicht zählen'
            else:
                # Verdächtig wenn nicht gezählt — z.B. echter Layover-Ort aber klass falsch
                if klass not in ('Z73','Z74','Z76') and layover_ort and layover_ort != hb_upper:
                    why_susp = f'overnight + Layover {layover_ort} ≠ Homebase, aber klass={klass}'

            if counted_h or why_susp:
                # v8.18.5: erweiterter Audit für Heimkehr-Erkennung
                ends_homebase_robust = bool(d.get('ends_at_homebase')) or (
                    (d.get('routing') or [''])[-1] or ''
                ).upper().strip() == hb_upper if d.get('routing') else False
                extra_hotelnaechte.append({
                    'datum':                       datum,
                    'klass':                       klass,
                    'marker':                      t_diag.get('marker', ''),
                    'routing':                     t_diag.get('routing', ''),
                    'layover_ort':                 layover_ort,
                    'overnight_after_day':         overnight,
                    'ends_at_homebase':            bool(d.get('ends_at_homebase')),
                    'ends_at_homebase_robust':     ends_homebase_robust,
                    'cluster_id':                  cluster.get('_id') if cluster else None,
                    'cluster_foreign':             cluster_foreign,
                    'z73_type':                    z73_type_diag,
                    'is_evening_foreign_tour_start': z73_type_diag == 'evening_foreign_tour_start',
                    'counted_as_hotel_nacht':      counted_h,
                    'reason_counted':              cr_diag.get('reason', '')[:120],
                    'why_suspicious':              why_susp,
                })

        # wrong_z72_candidates: Z72 das verletzt sein könnte
        if klass == 'Z72':
            if overnight or d.get('has_fl') or prev_overnight or i in cluster_for_idx:
                wrong_z72_candidates.append({
                    'datum': datum,
                    'overnight': overnight,
                    'has_fl': bool(d.get('has_fl')),
                    'prev_overnight': prev_overnight,
                    'in_cluster': i in cluster_for_idx,
                    'reason': 'Z72-Hard-Gate möglicherweise verletzt'
                })

        # missing_z73_candidates: echter Inland-Layover (≠ Homebase) ohne Z73/Z74.
        # v8.8: Konsistent mit Classifier — SE-Ort hat Vorrang vor DP-layover_ort.
        # Sonst false-positive bei Tagen mit Auslands-SE-Stempel + DP-Inland-layover.
        effective_ort = (se_ort or layover_ort).upper()
        if (overnight and effective_ort and effective_ort != hb_upper
                and _is_inland_code(effective_ort) and klass not in ('Z73', 'Z74')):
            missing_z73_candidates.append({
                'datum': datum, 'klass': klass, 'layover_ort': effective_ort,
                'reason': f'Inland-Layover {effective_ort} (≠ Homebase) ohne Z73/Z74'
            })

        # missing_z76_candidates: Auslands-SE-Zeile oder Foreign-Cluster aber kein Z76
        # v8.12: Z73 mit Abend-Anreise-Reason ist legitim (nicht missing_z76).
        is_legitimate_z73_evening = klass == 'Z73' and 'Abend-Briefing' in (
            tage_detail[i].get('begruendung','') if i < len(tage_detail) else '')
        if klass not in ('Z76',) and not is_legitimate_z73_evening:
            if has_active_se and se_inland is False:
                missing_z76_candidates.append({
                    'datum': datum, 'klass': klass, 'se_ort': se_ort,
                    'reason': 'Aktive Auslands-SE-Zeile aber klass != Z76'
                })
            elif overnight and layover_ort and not _is_inland_code(layover_ort):
                missing_z76_candidates.append({
                    'datum': datum, 'klass': klass, 'layover_ort': layover_ort,
                    'reason': f'Auslands-Layover {layover_ort} aber klass != Z76'
                })

        # v8.9: missing_deutschland_14_candidates — Tage die EVTL. Z72/Z73-Inland
        # sein müssten (Deutschland 14€). Heuristik:
        # - Same-Day mit Inland-SE-Stempel + Dauer ≥ 8h → Z72-Kandidat (wenn nicht Z72)
        # - Tour-Anreise mit Inland-Layover (≠ Homebase) → Z73-Kandidat (wenn nicht Z73)
        # Aktuelle Klass darf NICHT bereits Z72/Z73/Z74 sein.
        if klass not in ('Z72', 'Z73', 'Z74'):
            duty_min_local = int(d.get('duty_duration_minutes') or 0)
            # Same-Day Inland >8h → Z72-Kandidat
            if (at == 'same_day' and not overnight and not prev_overnight
                    and (se_inland is True or (effective_ort and _is_inland_code(effective_ort)))
                    and (duty_min_local + commute_minutes * 2) >= SAME_DAY_Z72_TOTAL_MINUTES):
                missing_deutschland_14_candidates.append({
                    'datum': datum, 'klass': klass, 'expected': 'Z72',
                    'effective_ort': effective_ort,
                    'duty_minutes': duty_min_local,
                    'reason': f'Same-Day Deutschland >8h → Z72-Kandidat (aktuell {klass})'
                })
            # Tour-Anreise mit Inland-Layover (≠ Homebase) → Z73-Kandidat
            elif (overnight and effective_ort and effective_ort != hb_upper
                  and _is_inland_code(effective_ort)
                  and not cluster_foreign):  # nur reiner Inland-Cluster
                missing_deutschland_14_candidates.append({
                    'datum': datum, 'klass': klass, 'expected': 'Z73',
                    'effective_ort': effective_ort,
                    'reason': f'Inland-Layover {effective_ort} im Inland-Cluster → Z73-Kandidat (aktuell {klass})'
                })

        # v8.12: aerotax_z76_dates_amounts — Bug-Fix: amount aus tage_detail.eur
        # statt aus eur_added (das ist im Diagnose-Loop am Ende nicht mehr aktuell).
        if klass == 'Z76':
            t_detail = tage_detail[i] if i < len(tage_detail) else {}
            cr = t_detail.get('classifier_result') or {}
            aerotax_z76_dates_amounts.append({
                'datum':       datum,
                'amount':      round(t_detail.get('eur', 0), 2),
                'layover_ort': effective_ort,
                'bmf_land':    cr.get('bmf_land', '') or '',
                'tagtyp':      cr.get('bmf_tagtyp', '') or '',
                'is_anreise':  i in cluster_for_idx and cluster_for_idx[i].get('indices', [None])[0] == i,
                'is_abreise':  i in cluster_for_idx and cluster_for_idx[i].get('indices', [None])[-1] == i,
            })

        # v8.11: office_z72_candidates — Office mit duty>=8h ohne klaren Homebase-Bezug
        if klass == 'Office':
            duty_min_office = int(d.get('duty_duration_minutes') or 0)
            if duty_min_office >= SAME_DAY_Z72_TOTAL_MINUTES:
                office_z72_candidates.append({
                    'datum': datum, 'klass': klass,
                    'marker': d.get('raw_marker', '') or at,
                    'duty_minutes': duty_min_office,
                    'reason': 'Office >8h — auswärtige Schulung/Training? Z72-Kandidat'
                })

        # v8.22 Now-4: Unknown-Marker-Kandidaten — Sonnet sieht raw_marker, kann
        # die Aktivität aber nicht zuordnen (activity_type=unknown). Wird im
        # review_items-Flow als "Was bedeutet diese Kennung?" präsentiert.
        if at == 'unknown':
            raw_mk = (d.get('raw_marker') or '').strip()
            if raw_mk and len(raw_mk) <= 30:
                # First-Token (das ist die Kennung, z.B. "SIM" aus "SIM SIMULATOR")
                first = raw_mk.split()[0] if raw_mk else ''
                if first and len(first) <= 8:
                    unknown_marker_candidates.append({
                        'datum': datum, 'marker': raw_mk, 'first_token': first,
                        'reason': 'Unbekannte Kennung — bitte erklären',
                    })

        # v8.21/v8.24: office_training_time_missing_candidates — Office/Training-Tag
        # der potenziell Z72 wäre, aber Reader hat keine Zeitinfo geliefert.
        # v8.24: Mehrtagige training_seq-Tage werden NICHT als Einzel-Kandidaten
        # rausgekippt — die Sequenz hat eigene Audit-Logik. Nur isolierte
        # Office/Training-Tage außerhalb erkannter Sequenzen werden zu Items.
        if klass == 'Office' and at in ('office', 'training'):
            raw_duty_rev = d.get('duty_duration_minutes')
            duty_known_rev = isinstance(raw_duty_rev, (int, float)) and raw_duty_rev > 0
            in_cluster_rev = i in cluster_for_idx
            has_active_foreign_se_rev = (
                se.get('count', 0) > 0
                and se.get('stfrei_inland') is False
                and bool(se.get('stfrei_ort'))
            )
            # v8.24: Tag ist Teil einer Mehrtages-Schulungssequenz (training_seq_skip)?
            # Wenn ja, NICHT einzeln fragen — nur Tag 1 der Sequenz wird zur Frage.
            in_training_seq_followup = i in training_seq_skip
            if (not duty_known_rev
                and not overnight and not prev_overnight
                and not in_cluster_rev
                and not has_active_foreign_se_rev
                and not in_training_seq_followup):
                office_training_time_missing_candidates.append({
                    'datum': datum,
                    'activity_type': at,
                    'marker': d.get('raw_marker', '') or at,
                    'reason': 'Office/Schulung an Homebase ohne Zeitinfo — Z72-Plausi unklar',
                    'money_impact_estimate': 14.0,  # potenzielle Z72-Pauschale
                })

    # v8.11: training_commute_candidates — mehrtägige Training-Sequenz
    # (≥TRAINING_SEQ_MIN_DAYS zusammen). Wenn so viele Tage hintereinander
    # activity_type=training mit requires_commute=true, ist es vermutlich EINE
    # auswärtige Schulung/Seminar mit nur 1 Anfahrt, nicht täglich eigene Fahrt.
    seq_start = None
    for i, m in enumerate(sorted_days):
        d = m['dp']
        if d.get('activity_type') == 'training' and d.get('requires_commute'):
            if seq_start is None:
                seq_start = i
        else:
            if seq_start is not None and (i - seq_start) >= TRAINING_SEQ_MIN_DAYS:
                training_commute_candidates.append({
                    'start_datum': sorted_days[seq_start]['datum'],
                    'end_datum':   sorted_days[i-1]['datum'],
                    'days_count':  i - seq_start,
                    'reason': f'Mehrtägige Training-Sequenz {sorted_days[seq_start]["datum"]} bis {sorted_days[i-1]["datum"]} — evtl. nur 1-2 Fahrtage statt jeden Tag'
                })
            seq_start = None
    if seq_start is not None and (len(sorted_days) - seq_start) >= TRAINING_SEQ_MIN_DAYS:
        training_commute_candidates.append({
            'start_datum': sorted_days[seq_start]['datum'],
            'end_datum':   sorted_days[-1]['datum'],
            'days_count':  len(sorted_days) - seq_start,
            'reason': 'Mehrtägige Training-Sequenz am Jahresende — evtl. nur 1-2 Fahrtage'
        })

    # v8.11: missing_reader_days — Tage in Datum-Range die der DP-Reader weggelassen hat.
    # Häufig sind das Frei/Urlaub/Krank-Tage die Sonnet weglassen sollte. Aber wenn ein
    # Tag mitten in einer aktiven Tour fehlt, ist das ein Reader-Bug.
    if sorted_days:
        try:
            from datetime import date as _date
            start_d = _date.fromisoformat(sorted_days[0]['datum'])
            end_d   = _date.fromisoformat(sorted_days[-1]['datum'])
            present = set(m['datum'] for m in sorted_days)
            cur = start_d
            while cur <= end_d:
                iso = cur.isoformat()
                if iso not in present:
                    missing_reader_days.append({'datum': iso, 'reason': 'Nicht im DP-Reader-Output'})
                cur = cur.replace(day=cur.day) if False else cur
                # Erhöhung: pythonisch
                cur = cur.fromordinal(cur.toordinal() + 1)
        except Exception:
            pass

    # Logging (kompakt, top-5 pro Liste)
    print(f"[v8-diag] extra_fahrtage={len(extra_fahrtage)} extra_arbeitstage={len(extra_arbeitstage)} "
          f"extra_hotelnaechte={len(extra_hotelnaechte)} wrong_z72={len(wrong_z72_candidates)} "
          f"missing_z73={len(missing_z73_candidates)} missing_z76={len(missing_z76_candidates)} "
          f"bmf_missing={len(_diag_bmf['bmf_missing'])} iata_unknown={len(set(_diag_bmf['iata_unknown']))}")
    for item in (extra_fahrtage + extra_arbeitstage + extra_hotelnaechte
                 + wrong_z72_candidates + missing_z73_candidates + missing_z76_candidates)[:30]:
        print(f"[v8-diag-item] {item}")
    for iata in sorted(set(_diag_bmf['iata_unknown'])):
        print(f"[v8-iata-unknown] {iata}")
    for entry in _diag_bmf['bmf_missing']:
        print(f"[v8-bmf-missing] {entry}")

    # Plausi-Issues (v8: Hard-Fails + Soft-Warnings)
    plausi_issues = []
    plausi_hard_fails = []

    # Hard-Fails (red): unmögliche Konstellationen
    if hotel_naechte > arbeitstage:
        plausi_hard_fails.append(f'Hotelnächte ({hotel_naechte}) > Arbeitstage ({arbeitstage}) — unplausibel')
    if arbeitstage > 230:
        plausi_hard_fails.append(f'Arbeitstage={arbeitstage} unplausibel hoch (>230)')

    # Soft-Warnings (yellow)
    if z72_tage == 0 and arbeitstage > 100:
        plausi_issues.append(f'Plausi: Z72=0 bei {arbeitstage} Arbeitstagen — Same-Day-Trips evtl. übersehen')
    if z73_tage == 0 and hotel_naechte > 40:
        plausi_issues.append(f'Plausi: Z73=0 bei {hotel_naechte} Hotelnächten — Inland-Übernachtungen evtl. übersehen')
    if vma_unmapped_se:
        plausi_issues.append(f'VMA-Unmapped-SE: {len(vma_unmapped_se)} aktive SE-Zeilen ohne Klassifikation')
    if z76_tage == 0 and arbeitstage > 50:
        plausi_issues.append(f'Plausi: Z76=0 bei {arbeitstage} Arbeitstagen — Auslandstage evtl. übersehen')
    if issue_tage > 5:
        plausi_issues.append(f'Plausi: {issue_tage} Issue-Tage — viele Tage konnten nicht eindeutig klassifiziert werden')
    if arbeitstage < 50 and len([s for s in (audit_notes or []) if 'aktive' in str(s).lower()]) > 30:
        plausi_issues.append(f'Plausi: Nur {arbeitstage} Arbeitstage bei vielen aktiven SE-Zeilen — DP unvollständig?')

    return {
        'arbeitstage': arbeitstage,
        'reinigungstage': reinigungstage,
        'fahr_tage': fahr_tage,
        'hotel_naechte': hotel_naechte,
        'z72_tage': z72_tage,
        'z73_tage': z73_tage,
        'z74_tage': z74_tage,
        'z76_tage': z76_tage,
        'z72_eur': z72_eur,
        'z73_eur': z73_eur,
        'z74_eur': z74_eur,
        'z76_eur': z76_eur,
        'issue_tage': issue_tage,
        'tage_detail': tage_detail,
        # v8.18.6: 'unklare_tage' enthält NUR echte unresolved_days + Hard-Fails.
        # Plausi-Soft-Warnings (Z72=0, Z76=0 etc.) werden separat in plausi_issues
        # geführt und nicht als "unklare Tage" angezeigt — sind Hinweise, keine Issues.
        'unklare_tage': unresolved_days + plausi_hard_fails,
        'audit_notes': audit_notes,
        'unresolved_days': unresolved_days,
        'vma_unmapped_se': vma_unmapped_se,
        'plausi_issues': plausi_issues,
        'plausi_hard_fails': plausi_hard_fails,
        # v8.6: Diagnose-Listen für Live-Audit
        'extra_fahrtage':         extra_fahrtage,
        'extra_arbeitstage':      extra_arbeitstage,
        'extra_hotelnaechte':     extra_hotelnaechte,
        'wrong_z72_candidates':   wrong_z72_candidates,
        'missing_z73_candidates': missing_z73_candidates,
        'missing_z76_candidates': missing_z76_candidates,
        'missing_deutschland_14_candidates': missing_deutschland_14_candidates,
        'aerotax_z76_dates_amounts':   aerotax_z76_dates_amounts,
        'rescues':                     rescues,
        'training_sequences':          training_seq_audit,
        'training_commute_candidates': training_commute_candidates,
        'office_z72_candidates':       office_z72_candidates,
        'office_training_time_missing_candidates': office_training_time_missing_candidates,
        'unknown_marker_candidates':                unknown_marker_candidates,
        'missing_reader_days':         missing_reader_days,
        'hotel_candidate_issues':      hotel_candidate_issues,
        'bmf_missing':            list(_diag_bmf['bmf_missing']),
        'iata_unknown':           sorted(set(_diag_bmf['iata_unknown'])),
        'nachweis': '',
        '_v7_used': True,
    }


def _sonnet_read_se_summary_v2(pdf_bytes_list, year=2025):
    """Sonnet liest SE-PDFs nur für SUMMEN (Z77, Z76, Anzahl Abrechnungen, Flugmonate).
    Tag-für-Tag-Klassifikation macht Opus separat. Hier nur einfache Aggregation."""
    if not pdf_bytes_list or not ANTHROPIC_KEY:
        return None
    pdf_bytes_list = _bytes_list(pdf_bytes_list)
    if not pdf_bytes_list:
        return None

    se_tool = {
        'name': 'submit_se_summen',
        'description': 'Liefere die Summen aus allen Streckeneinsatz-Abrechnungen + monatliche Aufschlüsselung zur Verifikation',
        'input_schema': {
            'type': 'object',
            'required': ['z77_total', 'flugmonate', 'monatliche_z77'],
            'properties': {
                'z77_total': {
                    'type': 'number',
                    'description': 'Z77 = Σ aller Werte in der "Steuerfrei"-Spalte über alle Tageszeilen. '
                                   'WICHTIG: Es gibt mehrere SUMME-Zeilen pro Monat im PDF. IGNORIERE diese SUMME-Zeilen '
                                   'für die Berechnung von z77_total. Summiere stattdessen alle Tageszeilen-stfrei-Werte selbst. '
                                   'Methode A (Tageszeilen-Summe) ist die einzige Wahrheit. '
                                   'Falls du eine Jahres-SUMME-Zeile siehst — gib sie NICHT als z77_total zurück, sondern '
                                   'gib sie nur als zusätzlichen Hinweis in monatliche_z77 (falls erkennbar pro Monat). '
                                   'Niemals Steuerpflichtig-Werte dazurechnen.'
                },
                'monatliche_z77': {
                    'type': 'array',
                    'items': {
                        'type': 'object',
                        'properties': {
                            'monat': {'type': 'integer', 'description': '1-12'},
                            'z77_monat': {'type': 'number', 'description': 'Σ stfrei-Werte für diesen Monat'},
                            'anzahl_zeilen': {'type': 'integer', 'description': 'Anzahl Tageszeilen für diesen Monat'}
                        },
                        'required': ['monat', 'z77_monat']
                    },
                    'description': 'Pro Monat: Z77-Anteil zur Cross-Verifikation. '
                                   'Σ(monatliche_z77) MUSS = z77_total ergeben.'
                },
                'summe_gesamt': {
                    'type': 'number',
                    'description': 'Σ aller GESAMT-Spalte (steuerpflichtig + steuerfrei zusammen)'
                },
                'summe_steuerpflichtig': {
                    'type': 'number',
                    'description': 'Σ aller "Steuerpflichtig"-Werte'
                },
                'flugmonate': {
                    'type': 'array',
                    'items': {'type': 'integer'},
                    'description': 'Liste der Monate (1-12) in denen Flüge stattfanden'
                },
                'anzahl_abrechnungen': {
                    'type': 'integer',
                    'description': 'Anzahl der erkannten Monatsabrechnungen (typisch 12 für volles Jahr)'
                },
                'auslandsspesen_total': {
                    'type': 'number',
                    'description': 'Σ aller stfrei-Werte wo stfrei-Ort AUSLAND ist (≠ FRA/MUC/HAM/STR/CGN/HAJ/BER/LEJ/NUE/BRE/DUS/etc.). '
                                   'WICHTIG: Inlandsspesen NIEMALS hier mitzählen. '
                                   'Mathematische Invariante: auslandsspesen_total + inlandsspesen_total ≈ z77_total (±5€ Rundung).'
                },
                'inlandsspesen_total': {
                    'type': 'number',
                    'description': 'Σ aller stfrei-Werte wo stfrei-Ort INLAND ist (FRA/MUC/HAM/STR/CGN/HAJ/BER/LEJ/NUE/BRE/DUS). '
                                   'WICHTIG: Auslandsspesen NIEMALS hier mitzählen. '
                                   'Mathematische Invariante: auslandsspesen_total + inlandsspesen_total ≈ z77_total (±5€ Rundung).'
                }
            }
        }
    }

    # Alle PDFs in einen Call (Sonnet kann viele Documents auf einmal)
    content = []
    for pdf_bytes in pdf_bytes_list[:14]:
        try:
            content.append({
                'type': 'document',
                'source': {'type': 'base64', 'media_type': 'application/pdf',
                           'data': base64.b64encode(pdf_bytes).decode()}
            })
        except: pass
    if not content:
        return None
    content.append({
        'type': 'text',
        'text': f"""Du bekommst Lufthansa Streckeneinsatz-Abrechnungen für Steuerjahr {year}.

═══ FORMAT EINER STRECKENEINSATZ-ABRECHNUNG ═══

Pro Monat eine Abrechnung mit:
- Header: "Erstellt DD.MM.YYYY" + Mandanten-Daten
- Tabelle mit Spalten:
  DATUM | AB | AN | SPESEN-€ | ORT | ZWÖLFTEL | STFREI-€ | STFREI-ORT | STEUER | WERBKO | DOPP | STORNO
  Eine Zeile pro Tag/Tour-Aktivität (typisch 5-30 Zeilen pro Monat)
- SUMME-Zeile am Ende mit drei Beträgen:
  "Summe: GESAMT_€  STEUERPFLICHTIG_€  STEUERFREI_€"
  → STEUERFREI_€ in dieser Summe-Zeile = Z77 für diesen Monat

═══ WAS DU LIEFERN MUSST ═══

1. Z77_TOTAL: Σ aller "Steuerfrei"-Werte über ALLE Monate + ALLE Tage.
   → Methode A: Σ aller Tageszeilen-stfrei-Werte
   → Methode B: Σ aller Monats-Summe-Zeilen-stfrei-Werte
   → Beide Methoden müssen gleichen Wert ergeben — wenn nicht, prüfe Storno-Zeilen!

2. MONATLICHE_Z77: Pro Monat (1-12) den Z77-Anteil + Anzahl Zeilen.
   Σ(monatliche_z77) MUSS = z77_total sein. Das ist deine Selbst-Prüfung.

3. AUSLANDSSPESEN_TOTAL: Σ stfrei-Werte wo stfrei-Ort = AUSLAND
   (z.B. JFK, GRU, BLR, ICN, JNB — nicht FRA/MUC/HAM).
   Das wird später als Z76-Anteil von Z77 verwendet.

4. INLANDSSPESEN_TOTAL: Σ stfrei-Werte wo stfrei-Ort = INLAND
   (FRA, MUC, HAM, BER, DUS, STR, CGN, HAJ, BRE, NUE, LEJ, etc.)
   Cross-Check: AUSLANDSSPESEN + INLANDSSPESEN = Z77_TOTAL.

5. FLUGMONATE: Liste 1-12 in denen Flüge stattfanden (DD.MM.YYYY-Zeilen).

═══ WICHTIGE REGELN ═══

❌ Storno-Zeilen (Spalte STORNO enthält "X") IGNORIEREN — nicht summieren.
❌ Steuerpflichtig-Spalte NICHT zu Z77 dazurechnen — Z77 ist NUR Steuerfrei.
❌ Bei Multi-Page-PDFs: ALLE Seiten lesen, nicht nur die erste.
✓ Bei Tageszeile mit nur einem Wert in Steuerfrei-Spalte: addiere diesen.
✓ Bei Tageszeile mit "0,00" oder leer in Steuerfrei: addiere 0 (zähle aber Zeile).
✓ Verifiziere durch zwei Methoden (Σ Zeilen vs Σ Summe-Zeilen) — bei Diskrepanz
   nimm den höheren Wert und prüfe nochmal Storno.

═══ REALISTIC-CHECK ═══

Z77 für Vollzeit-LH-Cabin-Crew typisch 3.000-7.000€/Jahr.
Wenn dein Z77 < 1.500€ oder > 10.000€: prüfe nochmal — wahrscheinlich
hast du Monate übersehen oder Storno-Zeilen mitgezählt.

Liefere via Tool das strukturierte Ergebnis."""
    })

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=180.0)
    try:
        resp = None
        for attempt in range(3):
            try:
                resp = client.messages.create(
                    model='claude-sonnet-4-6', max_tokens=2000,
                    tools=[se_tool],
                    tool_choice={'type': 'tool', 'name': 'submit_se_summen'},
                    messages=[{'role': 'user', 'content': content}]
                )
                break
            except Exception as e:
                if attempt == 2: raise
                print(f"[Sonnet-SE] retry {attempt+1}/3: {str(e)[:100]}")
                import time as _t; _t.sleep(2 ** attempt + 1)
        tool_input = None
        for block in resp.content:
            if getattr(block, 'type', None) == 'tool_use':
                tool_input = block.input
                break
        if not tool_input:
            print(f"[Sonnet-SE] kein tool_input")
            return None
        # v10.3: choose_z77_source — drei Quellen, klare Priorität.
        # Hier verfügbar: monatliche_z77 (B) + declared (C). daily_lines hat dieser
        # Reader nicht; structured-Reader liefert die separat.
        monatliche = tool_input.get('monatliche_z77', []) or []
        z77_declared = float(tool_input.get('z77_total', 0) or 0)
        z77_choice = choose_z77_source(daily_lines=0,
                                       monthly_z77_list=monatliche,
                                       declared_total=z77_declared)
        z77_main = z77_choice['z77_used']
        # Logging: notes als INFO, nur echte warnings als ⚠
        for n in z77_choice['z77_audit']['notes']:
            print(f"[Sonnet-SE] info: {n}")
        for w in z77_choice['warnings']:
            print(f"[Sonnet-SE] ⚠ {w}")

        ausland_se = float(tool_input.get('auslandsspesen_total', 0) or 0)
        inland_se  = float(tool_input.get('inlandsspesen_total', 0) or 0)
        # Konsistenz-Check: Inland + Ausland sollte ≈ Z77 sein.
        # v10.3: kleine Diff → INFO/audit-note. Große Diff → echte Skalierung mit Note.
        ai_sum = ausland_se + inland_se
        ia_audit_note = None
        if ai_sum > 0 and abs(ai_sum - z77_main) > 50:
            ia_audit_note = (
                f"Inland({inland_se:.2f}) + Ausland({ausland_se:.2f}) = {ai_sum:.2f}€ "
                f"≠ Z77 ({z77_main:.2f}€). Skaliert proportional auf Z77."
            )
            if abs(ai_sum - z77_main) > max(200.0, z77_main * 0.10):
                # Große Diff: echte Warning
                print(f"[Sonnet-SE] ⚠ Inland/Ausland-Inkonsistenz: {ia_audit_note}")
            else:
                # Kleine bis mittlere Diff: nur info
                print(f"[Sonnet-SE] info: {ia_audit_note}")
            if ai_sum > 0:
                scale = z77_main / ai_sum
                ausland_se = round(ausland_se * scale, 2)
                inland_se  = round(inland_se  * scale, 2)
        result = {
            'z77_total': z77_main,
            'z77_audit': dict(z77_choice['z77_audit'],
                              inland_ausland_note=ia_audit_note),
            'summe_gesamt': float(tool_input.get('summe_gesamt', 0) or 0),
            'summe_steuerpflichtig': float(tool_input.get('summe_steuerpflichtig', 0) or 0),
            'auslandsspesen_total': ausland_se,
            'inlandsspesen_total': inland_se,
            'flugmonate': sorted(set(int(m) for m in tool_input.get('flugmonate', []) if 1 <= int(m) <= 12)),
            'anzahl_abrechnungen': int(tool_input.get('anzahl_abrechnungen', 0) or 0),
            'monatliche_z77': monatliche,
        }
        print(f"[Sonnet-SE] Z77={result['z77_total']:.2f}€ src={z77_choice['z77_source']} "
              f"(Inland {result['inlandsspesen_total']:.2f}€ + Ausland {result['auslandsspesen_total']:.2f}€) "
              f"Abrechnungen={result['anzahl_abrechnungen']} Flugmonate={result['flugmonate']}")
        return result
    except Exception as e:
        print(f"[Sonnet-SE] fail: {e}")
        return None


def _opus_classify_days_v2(dp_bytes, einsatz_bytes, se_bytes, year=2025, homebase='FRA', feedback=None):
    """Opus 4.7: liest Dienstplan + Einsatzplan + SE parallel, klassifiziert Tag für Tag.
    Liefert: arbeitstage, fahrtage, hotel_naechte, z72/73/74_tage und EUR, z76_eur,
    plus monatlichen Nachweis. Konservative branchenüblicher Steuerberater-Methode.

    feedback: Optional dict {'prev_classification': cls, 'issues': [str]} — bei
    Self-Reflection-Pass wird Opus mit konkreten Hinweisen zur Korrektur erneut aufgerufen."""
    if not ANTHROPIC_KEY:
        return None
    dp_bytes = _bytes_list(dp_bytes) if dp_bytes else []
    einsatz_bytes = _bytes_list(einsatz_bytes) if einsatz_bytes else []
    se_bytes = _bytes_list(se_bytes) if se_bytes else []
    if not dp_bytes:
        return None  # ohne Dienstplan nicht möglich

    # Alle PDFs als Document-Content
    content = []
    pdf_count = 0
    for label, blist in [('Dienstplan', dp_bytes), ('Einsatzplan', einsatz_bytes), ('Streckeneinsatz', se_bytes)]:
        for pdf_bytes in blist[:14]:
            try:
                content.append({
                    'type': 'document',
                    'source': {'type': 'base64', 'media_type': 'application/pdf',
                               'data': base64.b64encode(pdf_bytes).decode()}
                })
                pdf_count += 1
            except: pass
    print(f"[Opus-Klassifikation] {pdf_count} PDFs an Opus übergeben")

    # Tool für strukturierte Antwort
    classify_tool = {
        'name': 'submit_tag_klassifikation',
        'description': 'Liefere die Tag-für-Tag-Auswertung der LH-Dokumente',
        'input_schema': {
            'type': 'object',
            'required': ['arbeitstage', 'fahr_tage', 'hotel_naechte',
                         'z72_tage', 'z73_tage', 'z74_tage', 'z76_eur', 'nachweis'],
            'properties': {
                'arbeitstage':   {'type': 'integer', 'description': 'Alle Tage mit Dienst (Tour/Office/Standby/Schulung), ohne Frei/Urlaub/Krank'},
                'fahr_tage':     {'type': 'integer', 'description': 'Tage an denen User zum Flughafen fahren musste (Tour-Start + Office-Day). Eine Tour = 1 Fahrtag total, egal wie lang.'},
                'hotel_naechte': {'type': 'integer', 'description': 'Σ ALLER Übernachtungen außerhalb Homebase (FL-Marker oder anderer Hotel-Indikator) — INLAND UND AUSLAND. Auch Inland-Schulungs-Hotels und Inland-Tour-Layovers zählen. NICHT nur Auslands-Übernachtungen!'},
                'z72_tage':      {'type': 'integer', 'description': 'Tagestrips Inland >8h (mit Briefingzeit-Berechnung) ohne Übernachtung'},
                'z72_eur':       {'type': 'number',  'description': 'z72_tage × 14€ (BMF Inland Tagestrip)'},
                'z73_tage':      {'type': 'integer', 'description': 'An-/Abreisetage Inland (mit Inland-Übernachtung). Auch Auslandstour-Anreise mit FRA-stfrei zählt hier (branchenüblicher Steuerberater-Methode konservativ).'},
                'z73_eur':       {'type': 'number',  'description': 'z73_tage × 14€'},
                'z74_tage':      {'type': 'integer', 'description': 'Inland 24h ohne Ab/An-Zeiten (selten)'},
                'z74_eur':       {'type': 'number',  'description': 'z74_tage × 28€'},
                'z76_eur':       {'type': 'number',  'description': 'Σ aller stfrei-Werte aus SE wo stfrei-Ort Ausland ist (Z76 VMA Ausland)'},
                'nachweis':      {'type': 'string',  'description': 'Monatlicher Nachweis. Pro Monat 1-3 Sätze: was waren die Touren, wie viele Arbeitstage/Fahrtage/Hotel.'},
                'unklare_tage':  {'type': 'array', 'items': {'type': 'string'}, 'description': 'Tage die nicht eindeutig klassifizierbar waren (mit Begründung)'},
                'tage_detail':   {
                    'type': 'array',
                    'description': 'Tag-für-Tag-Klassifikation NUR für Tour-/Office-/Standby-Tage (NICHT für FREI/Urlaub/Krank). Pro Tour: nur den ANREISE-Tag eintragen (mit dauer-Hinweis). Bei Same-Day: 1 Eintrag. So bleibt die Liste kompakt (~50-80 Einträge/Jahr).',
                    'items': {
                        'type': 'object',
                        'required': ['datum', 'marker', 'klass', 'begruendung'],
                        'properties': {
                            'datum':       {'type': 'string', 'description': 'YYYY-MM-DD'},
                            'marker':      {'type': 'string', 'description': 'DP-Marker am Tag (FL/SBY/RES/EM/EH/D4/...)'},
                            'routing':     {'type': 'string', 'description': 'Routing-Codes z.B. "FRA-CPH-FRA" oder "FRA-BLR" (bei Übernachtung) — wenn unklar leer'},
                            'klass':       {'type': 'string', 'enum': ['Z72', 'Z73', 'Z74', 'Z76', 'Office', 'Standby', 'Sonstiges'], 'description': 'Klassifikation für VMA-Berechnung'},
                            'tour_dauer':  {'type': 'integer', 'description': 'Anzahl Tage der Tour (1=Same-Day, 2-5=mehrtägig). Bei Office/Standby = 1.'},
                            'begruendung': {'type': 'string', 'description': 'Kurz: warum diese Klassifikation? z.B. "Same-Day CPH+Rückflug → Z72" oder "BLR Indien 3 Tage → Z76 Auslandstour" oder "DUS Übernachtung → Z73 Inland-ÜN"'},
                        }
                    }
                },
            }
        }
    }

    # Wissens-Buch laden — Single Source of Truth.
    # NUR referenz_faelle.txt laden (konsolidiert: §9 EStG, §3 Nr. 16, EASA-FTL,
    # BMF-Pauschalen, LH-Marker-Katalog, branchenüblicher Standard, Reference-Cases,
    # Häufige Fehler). Die alte referenz_easa.txt wird NICHT mehr geladen weil sie
    # widersprüchliche VMA-Klassifikations-Logik enthält.
    wissensbuch = ''
    try:
        ref_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_faelle.txt')
        if os.path.exists(ref_path):
            with open(ref_path, encoding='utf-8') as f:
                wissensbuch = f.read()
    except Exception as e:
        print(f"[Opus-Klassifikation] Wissens-Buch laden fail: {e}")

    prompt = f"""Du bist erfahrener Werbungskosten-Klassifikator spezialisiert auf Lufthansa-Kabinenpersonal (branchenübliche Steuer-Praxis).
Mandant: LH-Cabin-Crew, Homebase {homebase}, Steuerjahr {year}.

═══════════════════════════════════════════════════════════════════════════
█ TEIL 1: VERPFLICHTENDES WISSEN — LIES UND VERINNERLICHE
═══════════════════════════════════════════════════════════════════════════

{wissensbuch}

═══════════════════════════════════════════════════════════════════════════
█ TEIL 2: DEIN ANALYSE-AUFTRAG
═══════════════════════════════════════════════════════════════════════════

Du bekommst {pdf_count} PDFs in dieser Reihenfolge:
- Dienstplan / Flugstunden-Übersichten (Tag-für-Tag-Marker)
- Einsatzplan / CAS-PUB-Liste (detaillierter mit Briefingzeiten, Routings)
- Streckeneinsatz-Abrechnungen (was LH stfrei gezahlt hat)

═══ STEP 1 — Datenbasis aufbauen ═══

Lies ALLE PDFs durch. Erstelle innerlich einen Tag-Kalender 1.1.{year}-31.12.{year}.
Pro Tag erfasse:
- Welcher Marker im Dienstplan? (FREI/U/K/A/E/FL/SBY/RES/EK/EM/D4/DD/BRIEFING/...)
- Welche Routing-Info im Einsatzplan? (Tour-Code, Briefingzeit, Block-Time)
- Welche stfrei-Werte in der SE? (mit stfrei-Ort: FRA/MUC/HAM = Inland, sonst Ausland)

═══ STEP 2 — Tag-Klassifikation ═══

Wende das DENK-SCHEMA aus Teil 1, Abschnitt 1 an:

Pro Tag:
1. FREI/U/K → kein Arbeitstag, fertig.
2. SBY/RES/Online-Schulung → Arbeitstag, kein Fahrtag, kein Hotel, fertig.
3. EK/EM/D4/DD/BRIEFING (Office-Day in {homebase}) → Arbeitstag + Fahrtag (täglich!).
4. Tour (A/E/FL):
   a. Same-Day-Heimkehr (A + E gleicher Tag, egal ob Inland oder EU-Ausland):
      → Z72 mit 14€ Inland-Tagestrip-Pauschale (LH/branchenüblicher Konvention)
      → 1 Fahrtag, 0 Hotelnacht
   b. Mehrtägig mit Hotel im INLAND (z.B. Schulung MUC mit Übernachtung):
      → Anreise-Tag = Z73 (14€), Volltage = nur Arbeitstag, Abreise-Tag = Z73 (14€)
      → 1 Fahrtag pro Tour, n Hotelnächte
   c. Mehrtägig mit Hotel im AUSLAND:
      → ALLE Tage = Z76, BMF-Auslands-Anreise-Pauschale für Tag 1 + letzten Tag,
        BMF-Auslands-24h-Pauschale für Volltage zwischen
      → 1 Fahrtag pro Tour, n Hotelnächte (FL-Marker)

═══ STEP 3 — Aggregation ═══

Zähle:
- arbeitstage = Σ aller Tage außer Frei/Urlaub/Krank
- fahr_tage = Σ Tour-Starts + Σ Office-Days
  (Eine Tour = 1 Fahrtag insgesamt, AUCH bei mehreren Etappen!)
- hotel_naechte = Σ FL-Marker (≥10h Bodenzeit)
- z72_tage = Σ Same-Day-Tagestrips mit ≥8h Abwesenheit
- z73_tage = Σ Inland-Mehrtages-Tour Anreise- + Abreise-Tage (NUR echte Inland-Touren!)
- z74_tage = Σ Inland 24h-Tage (sehr selten, typisch 0-2/Jahr)
- z76_eur = Σ BMF-Auslands-Pauschalen pro Auslandstour-Tag
  (Anreise-Satz für Tag 1+letzten, 24h-Satz für Volltage)

═══ STEP 4 — Self-Check VOR Liefern ═══

PFLICHT vor dem Tool-Aufruf — gehe diese Checks durch:

✓ Z76_EUR > Z77? Das ist ein starkes Audit-Warnsignal, aber kein automatischer
  Rechts-/Rechenfehler. Prüfe dann SE-Vollständigkeit, Auslands-/Inland-
  Klassifikation, Storno-Zeilen, Zwölftel/Kürzungen und gestellte Mahlzeiten.
  Z76 NICHT pauschal auf Z77 deckeln.

✓ hotel_naechte ≤ arbeitstage (logisch zwingend)

✓ fahr_tage ≤ arbeitstage (logisch zwingend)

✓ z73_tage typisch 5-15 (NUR echte Inland-Touren mit Hotel — z.B. Schulungen).
  Wenn dein Z73 > 20: hast du Auslandstouren als Z73 reklassifiziert? FALSCH!

✓ Plausi-Bandbreite Vollzeit: arbeitstage 120-160, fahrtage 50-80, hotel 50-80.
  Wenn deutlich darüber/unter: prüfe ob du SBY/RES korrekt gezählt hast oder
  Office-Days als Fahrtag erkannt hast.

Wenn ein Check fehlschlägt: zurück zu STEP 2 und nachschärfen.

═══ STEP 5 — Nachweis schreiben ═══

Im 'nachweis'-Feld: Monat für Monat 2-3 Sätze. Format-Beispiel:
"JAN: 5 Touren — BLR-Tour 03-06.01 (Z76 Indien), 2× CPH-Tagestrip (Z72), HKG-Tour
17-21.01 (Z76 China), 1× DD-Schulung Tag 28 (Office). 12 AT, 6 FT, 5 Hotel."

Bei unklaren Tagen: in 'unklare_tage' mit Begründung listen. NIE raten.

═══ ANTI-MUSTER (NICHT MACHEN) ═══

❌ Auslandstour-Anreise-Tag mit FRA-Stempel als Z73 zählen (= Klassifikations-FEHLER #13)
❌ Same-Day-Tagestrip nach CPH als Z76 zählen (= Klassifikations-FEHLER #13)
❌ Inland-Schulung mit Hotel als Auslandstour klassifizieren (= FEHLER #14)
❌ DD/EK/EM nur 1 Fahrtag zählen statt täglich (= FEHLER #4 + #16)
❌ Plausi-Bandbreiten als Schätzung verwenden — IMMER zählen aus Dienstplan
❌ Werte ohne Tag-Beleg im Nachweis erfinden — bei Unsicherheit unklar markieren

LIEFERE jetzt via Tool die strukturierten Werte + monatlichen Nachweis."""

    # Self-Reflection-Pass: wenn vorheriger Klass-Output Math-Invarianten verletzt,
    # bekommt Opus die konkreten Issues als RE-KLASSIFIKATIONS-Auftrag mit.
    # WICHTIG: Recheck darf NICHT auf "Z76 runter" optimieren — sondern auf KORREKTE
    # Tour-Klassifikation. Z76 zu hoch heißt: zu viele Tage als Auslandstour ODER
    # zu wenige als Inland-Übernachtung, NICHT "alle in Z72 schieben".
    if feedback and feedback.get('issues'):
        prev = feedback.get('prev_classification', {}) or {}
        issues_list = feedback.get('issues', [])
        prompt += "\n\n═══════════════════════════════════════════════════════════════════════════"
        prompt += "\n█ RE-KLASSIFIKATIONS-AUFTRAG: deine vorherige Klassifikation hatte Probleme"
        prompt += "\n═══════════════════════════════════════════════════════════════════════════\n"
        prompt += f"\nDeine erste Klassifikation lieferte:"
        prompt += f"\n  arbeitstage={prev.get('arbeitstage')}, fahr_tage={prev.get('fahr_tage')}, "
        prompt += f"hotel={prev.get('hotel_naechte')}"
        prompt += f"\n  Z72={prev.get('z72_tage')}T, Z73={prev.get('z73_tage')}T, "
        prompt += f"Z74={prev.get('z74_tage')}T, Z76={prev.get('z76_eur'):.2f}€" if prev.get('z76_eur') is not None else ""
        prompt += "\n\nProbleme die das Backend identifiziert hat:\n"
        for i, iss in enumerate(issues_list, 1):
            prompt += f"  {i}. {iss}\n"
        prompt += "\n█ RECHECK-PRINZIP (KRITISCH — vermeide häufigen Reflex-Fehler!) ███████████\n"
        prompt += "\nZiel ist NICHT 'Z76 reduzieren um jeden Preis'. Ziel ist KORREKTE Tour-\n"
        prompt += "Klassifikation. Wenn dein erster Pass z.B. Z76>Z77 ergeben hat, ist die\n"
        prompt += "Lösung NICHT 'mehr Tage als Z72 markieren', sondern systematisch prüfen:\n\n"
        prompt += "  1. MULTI-STOP-TOUREN TAG-FÜR-TAG klassifizieren!\n"
        prompt += "     Eine Tour darf NIEMALS pauschal komplett als Z76 abgerechnet werden,\n"
        prompt += "     nur weil irgendwo im Verlauf ein Auslands-Layover vorkommt. Pro Kalender-\n"
        prompt += "     tag entscheidet der TATSÄCHLICHE Übernachtungsort:\n"
        prompt += "     • Inland-Layover (z.B. BER, MUC, DUS) → Z73 für An/Abreisetag dieser\n"
        prompt += "       Inland-Übernachtung. NICHT Z76 nur weil andere Tage der Tour Ausland sind.\n"
        prompt += "     • Ausland-Layover → Z76 für diesen Tag.\n"
        prompt += "     • Same-Day-Turnaround → Z72 (nur wenn Hard-Gate-Bedingungen alle erfüllt).\n"
        prompt += "     Bei Multi-Stop FRA→BER→ZAG→ARN→FRA: Tag 1 (BER-Übernachtung) ist Z73,\n"
        prompt += "     erst danach beginnt die Z76-Phase mit dem Übergang ins Ausland.\n\n"
        prompt += "  2. Sind ALLE als Z72 klassifizierten Tage echte Same-Day-Tagestrips?\n"
        prompt += "     Z72 IST NUR ZULÄSSIG wenn ALLE Bedingungen erfüllt:\n"
        prompt += "     • A-Marker UND E-Marker am SELBEN Kalendertag\n"
        prompt += "     • KEIN FL-Marker (Layover) am Tag oder Folgetag\n"
        prompt += "     • KEINE Heimkehr aus einer mehrtägigen Tour am selben Tag\n"
        prompt += "     • KEIN vorheriger oder folgender Tag gehört zur gleichen Tour\n"
        prompt += "     • KEIN Hotel-/Layover-Indiz im Einsatzplan\n"
        prompt += "     • Gesamtabwesenheit >8h\n"
        prompt += "     Wenn EINE Bedingung fehlt → NICHT Z72 → prüfe Z73 oder Z76.\n"
        prompt += "     Tag mit Heimkehr aus mehrtägiger Tour + neuer Same-Day = MISCHFALL: NIEMALS\n"
        prompt += "     pauschal nur Z72. Tour-Abschluss separat (Z76- oder Z73-Abreise) klassifizieren.\n\n"
        prompt += "  3. Wurden Inland-Übernachtungen (Z73) fälschlich als Z72, Z76 oder Office\n"
        prompt += "     klassifiziert? Prüfe besonders:\n"
        prompt += "     • Mehrtages-Schulungen (D4/DD/EM/EH): 2+ aufeinanderfolgende Schulungstage\n"
        prompt += "       OHNE FREI dazwischen + auswärtiger Schulungsort = Z73 für An/Abreise.\n"
        prompt += "     • Inland-Layovers in MUC/HAM/BER/DUS/STR/CGN/HAJ/NUE/LEJ/BRE als Teil\n"
        prompt += "       von Multi-Stop-Touren — diese Tage sind Z73, nicht Z76 nur weil später\n"
        prompt += "       Auslandstage folgen.\n\n"
        prompt += "  4. PASSEN die Hotelnächte zur Touren-Anzahl?\n"
        prompt += "     Hotelnächte sollten ≈ Σ(Auslands-Layovers + Inland-Schulungs-Übernachtungen).\n"
        prompt += "     Wenn Hotelnächte deutlich niedriger als FL-Marker im DP → Inland-\n"
        prompt += "     Übernachtungen übersehen oder Mehrtages-Schulung als Office gezählt.\n\n"
        prompt += "  5. Z76 ≤ Z77 ist KEIN Selbstzweck — wenn Z76 nach korrekter Re-Klassifikation\n"
        prompt += "     immer noch leicht über Z77 liegt, ist das OK (Audit-Hinweis im Backend).\n"
        prompt += "     LIEBER fachlich richtig + Hinweis als pauschal Z76 zerschnitten.\n\n"
        prompt += "█ REFLEX-FEHLER DEN DU NICHT MACHEN DARFST ████████████████████████████████\n"
        prompt += "\n❌ Multi-Stop-Tour pauschal komplett als Z76 abrechnen, weil Ausland vorkommt = FALSCH\n"
        prompt += "❌ Z76 war zu hoch → 'verschiebe Auslandstour-Tage einfach nach Z72' = FALSCH\n"
        prompt += "❌ Hotelnächte reduzieren um Z76 zu senken = FALSCH (verstößt gegen FL-Marker)\n"
        prompt += "❌ Z73 = 0 lassen 'weil sicherer' = FALSCH bei vorhandenen Inland-Layovers\n"
        prompt += "❌ Heimkehr-Tag aus mehrtägiger Tour + Same-Day pauschal nur als Z72 = FALSCH\n\n"
        prompt += "Im 'tage_detail' dokumentiere für JEDEN als Z72 markierten Tag explizit:\n"
        prompt += "'Same-Day-Heimkehr nachgewiesen durch A+E am Tag X, kein FL, keine Tour-Fortsetzung'.\n"
        prompt += "Im 'tage_detail' für Z73-Tage: 'Inland-Layover XYZ am Tag X mit Hotel'.\n"
        prompt += "Im 'tage_detail' für Z76-Tage: 'Auslandstour LAND, Hotel im Ausland'.\n"
        prompt += "Bei Mischfällen (Heimkehr + Same-Day): beide Komponenten getrennt aufführen.\n\n"
        prompt += "Liefere das fachlich KORREKT re-klassifizierte Ergebnis via Tool."

    content.append({'type': 'text', 'text': prompt})

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY, timeout=300.0)
    import time as _t
    start = _t.time()
    try:
        resp = None
        for attempt in range(2):
            try:
                resp = client.messages.create(
                    model='claude-opus-4-7', max_tokens=12000,
                    tools=[classify_tool],
                    tool_choice={'type': 'tool', 'name': 'submit_tag_klassifikation'},
                    messages=[{'role': 'user', 'content': content}]
                )
                break
            except Exception as e:
                if attempt == 1: raise
                print(f"[Opus-Klassifikation] retry: {str(e)[:100]}")
                _t.sleep(5)
        elapsed = _t.time() - start
        tool_input = None
        for block in resp.content:
            if getattr(block, 'type', None) == 'tool_use':
                tool_input = block.input
                break
        if not tool_input:
            print(f"[Opus-Klassifikation] kein tool_input — content blocks: {[getattr(b,'type',None) for b in resp.content]}")
            return None
        result = {
            'arbeitstage':   int(tool_input.get('arbeitstage', 0) or 0),
            'fahr_tage':     int(tool_input.get('fahr_tage', 0) or 0),
            'hotel_naechte': int(tool_input.get('hotel_naechte', 0) or 0),
            'z72_tage':      int(tool_input.get('z72_tage', 0) or 0),
            'z73_tage':      int(tool_input.get('z73_tage', 0) or 0),
            'z74_tage':      int(tool_input.get('z74_tage', 0) or 0),
            'z76_eur':       float(tool_input.get('z76_eur', 0) or 0),
            'nachweis':      str(tool_input.get('nachweis', '') or ''),
            'unklare_tage':  list(tool_input.get('unklare_tage', []) or []),
            'tage_detail':   list(tool_input.get('tage_detail', []) or []),
        }
        # EUR aus Tagen × BMF-Pauschale (vereinheitlicht)
        bmf = BMF_INLAND_BY_YEAR.get(year, BMF_INLAND_BY_YEAR[2025])
        result['z72_eur'] = round(result['z72_tage'] * bmf['tagestrip_8h'], 2)
        result['z73_eur'] = round(result['z73_tage'] * bmf['an_abreise'], 2)
        result['z74_eur'] = round(result['z74_tage'] * bmf['voll_24h'], 2)
        result['z76_eur'] = round(result['z76_eur'], 2)
        pass_label = '[Opus-Klassifikation-RECHECK]' if feedback else '[Opus-Klassifikation]'
        print(f"{pass_label} {elapsed:.1f}s: arbeit={result['arbeitstage']}T fahr={result['fahr_tage']}T "
              f"hotel={result['hotel_naechte']}T  Z72={result['z72_tage']}T/{result['z72_eur']:.2f}€  "
              f"Z73={result['z73_tage']}T/{result['z73_eur']:.2f}€  Z74={result['z74_tage']}T/{result['z74_eur']:.2f}€  "
              f"Z76={result['z76_eur']:.2f}€  unklar={len(result['unklare_tage'])}")
        return result
    except Exception as e:
        print(f"[Opus-Klassifikation] fail: {e}")
        return None


def _detect_classification_issues(cls, se_summary):
    """Prüft harte logische Invarianten und Audit-Plausibilitätschecks.
    Liefert konkrete Issue-Strings für den Self-Reflection-Pass.

    Harte Invarianten: Hotelnächte/Fahrtage dürfen Arbeitstage nicht übersteigen.
    Audit-Plausibilität: Z76 > Z77 oder starke Abweichung zu Auslandsspesen
    ist ein Recheck-Signal, aber kein automatischer mathematischer Beweisfehler.
    """
    if not cls or not se_summary:
        return []
    issues = []
    z76 = float(cls.get('z76_eur', 0) or 0)
    z77 = float(se_summary.get('z77_total', 0) or 0)
    auslandsspesen_se = float(se_summary.get('auslandsspesen_total', 0) or 0)
    arbeitstage = int(cls.get('arbeitstage', 0) or 0)
    # v8.17: reinigungstage als getrennter Counter (Uniform-/Dienstkleidungstage)
    reinigungstage = int(cls.get('reinigungstage', arbeitstage) or arbeitstage)
    fahr_tage = int(cls.get('fahr_tage', 0) or 0)
    hotel = int(cls.get('hotel_naechte', 0) or 0)
    z72_tage = int(cls.get('z72_tage', 0) or 0)
    z73_tage = int(cls.get('z73_tage', 0) or 0)

    # 1) Audit-Plausibilität: Z76 > Z77 ist kein harter Rechts-/Rechenfehler,
    # aber ein starkes Warnsignal für fehlende SE-Dateien, falsche Tourklassifikation
    # oder AG-Kürzungen/Mahlzeiten/Zwölftel-Logik. Nicht pauschal auf Z77 deckeln.
    if z77 > 0 and z76 > z77:
        issues.append(
            f"Audit-Warnung: Z76 = {z76:.2f}€ liegt über Z77 = {z77:.2f}€. "
            f"Bitte SE-Vollständigkeit, Auslands-/Inland-Klassifikation, Storno-Zeilen, "
            f"Zwölftel/Kürzungen und gestellte Mahlzeiten prüfen. Z76 nicht automatisch "
            f"auf Z77 deckeln."
        )
    # 2) Z76 vs Auslandsspesen-Summe ±30% — sollten ähnlich sein (BMF ≈ AG-Auszahlung pro Auslands-Tag)
    if auslandsspesen_se > 100 and z76 > 0:
        diff_pct = abs(z76 - auslandsspesen_se) / max(auslandsspesen_se, 1)
        if diff_pct > 0.40:  # >40% Diff = sehr suspekt
            direction = "ZU HOCH" if z76 > auslandsspesen_se else "ZU NIEDRIG"
            issues.append(
                f"Z76 = {z76:.2f}€ ist {direction} im Vergleich zu Auslandsspesen-SE = "
                f"{auslandsspesen_se:.2f}€ (Diff {diff_pct*100:.0f}%). "
                f"{'Du hast vermutlich Tage falsch als Auslandstour klassifiziert die eigentlich Inland sind.' if z76 > auslandsspesen_se else 'Du hast vermutlich Auslandstage übersehen oder als Inland klassifiziert.'}"
            )
    # 3) Hotel ≤ Arbeitstage (logisch zwingend)
    if hotel > arbeitstage:
        issues.append(
            f"Hotel-Nächte ({hotel}) > Arbeitstage ({arbeitstage}) — logisch unmöglich. "
            f"Eine Nacht-Übernachtung setzt einen Arbeitstag voraus."
        )
    # 4) Fahr-Tage ≤ Arbeitstage
    if fahr_tage > arbeitstage:
        issues.append(
            f"Fahr-Tage ({fahr_tage}) > Arbeitstage ({arbeitstage}) — logisch unmöglich. "
            f"Eine Tour = 1 Fahrtag, kein eigener Fahrtag pro Etappe."
        )
    # 5) Z72 viel + Z73=0 + viele Hotelnächte → Klassisches Anti-Muster: Inland-
    # Übernachtungen werden als Same-Day fehlklassifiziert. Häufiger Bug bei
    # Recheck-Pass der nur Z76 reduziert ohne Z73 zu erhöhen.
    if z72_tage > 20 and z73_tage == 0 and hotel > 30:
        issues.append(
            f"Anti-Muster: Z72={z72_tage} Tage + Z73=0 + Hotel={hotel} Nächte. "
            f"Bei {hotel} Hotelnächten und {z72_tage} 'Same-Day-Trips' fehlt Z73 wahrscheinlich. "
            f"Mehrtages-Inland-Touren oder Schulungen mit Hotel werden vermutlich als Z72 oder "
            f"Office klassifiziert. Prüfe alle EM/EH/D4-Blöcke und Inland-Layovers neu — "
            f"jede Tour mit Folgetag-Aktivität ohne FREI ist KEIN Z72."
        )
    # 6) Hotelnächte stark unter Z76-Tagen impliziert: zu viele Auslandstage erkannt OHNE Hotel-Nachweis.
    # Erwartung: Bei 4-Tages-Auslandstour sind 4 Z76-Tage und 3 Hotelnächte → Verhältnis ~1.33.
    # Verhältnis > 2.0 ist deutlich verdächtig (z.B. 90 Z76-Tage bei nur 30 Hotelnächten).
    if z76 > 0 and hotel > 0:
        z76_tage_geschaetzt = z76 / 50  # grobe Schätzung: Z76 in EUR / 50€ ø-Tagessatz
        if z76_tage_geschaetzt > hotel * 2.0:
            issues.append(
                f"Z76 = {z76:.0f}€ entspricht ~{z76_tage_geschaetzt:.0f} Auslandstagen, aber nur "
                f"{hotel} Hotelnächte erfasst (Verhältnis {z76_tage_geschaetzt/max(hotel,1):.1f}, "
                f"normal ~1.3). Auslandstour braucht Hotelnächte als Nachweis — FL-Marker "
                f"im Dienstplan vollständig erfasst?"
            )

    # 7) Pendel-Anti-Pattern: Z72 = 0 trotz Arbeitstagen → Hard-Gate zu strikt interpretiert.
    # Vollzeit-Crew hat fast immer mind. 1-2 Same-Day-Trips/Jahr. Z72=0 ist sehr unwahrscheinlich
    # bei Tour-aktiver Crew.
    if z72_tage == 0 and arbeitstage > 100 and z76 > 1000:
        issues.append(
            f"Anti-Muster: Z72 = 0 bei {arbeitstage} Arbeitstagen und Z76 = {z76:.0f}€. "
            f"Eine Tour-aktive Crew hat typisch mind. ein paar Same-Day-Tagestrips. Z72 = 0 deutet "
            f"darauf hin dass das Hard-Gate zu strikt interpretiert wurde — Tagestrips wurden "
            f"vermutlich fälschlich als Office oder gar nicht klassifiziert. Prüfe alle Tage mit "
            f"A+E-Markern am selben Kalendertag ohne FL erneut."
        )
    return issues


def hybrid_analyze(form, files, job_id=None):
    """Hauptanalyse: Sonnet (LSB+SE-Summen) + Opus (Tag-Klassifikation) SEQUENZIELL.
    Sequenziell statt parallel — schont Memory auf Render Free 512 MB.
    Nach jedem Call: gc.collect() + malloc_trim → maximaler Spike <500 MB.
    Rückgabe: {'lsb': {...}, 'se_summary': {...}, 'classification': {...}, 'errors': [...]}"""
    year = int(form.get('year', 2025))
    homebase = _extract_homebase(form.get('base', 'Frankfurt (FRA)'))

    lsb_bytes = []
    for item in (files.get('lsb') or []):
        lsb_bytes.append(item[0] if isinstance(item, tuple) else item)
    se_bytes = []
    for item in (files.get('se') or []):
        se_bytes.append(item[0] if isinstance(item, tuple) else item)
    dp_bytes = []
    for item in (files.get('dp') or []):
        dp_bytes.append(item[0] if isinstance(item, tuple) else item)
    # Einsatzplan ist seit v7 nicht mehr Pflicht und wird nicht aktiv genutzt.
    # Falls der Frontend-Upload-Flow legacy noch Bytes mitschickt: hier ignoriert.
    einsatz_bytes = []

    print(f"[v8] Start: LSB={len(lsb_bytes)} SE={len(se_bytes)} Flugstunden={len(dp_bytes)}")

    errors = []

    # Schritt 1: LSB (Default Sonnet, Local-First nur per ENV-Flag)
    lsb_data = None
    if lsb_bytes:
        try:
            _heartbeat_phase(job_id, 'lsb',
                             {'label': 'Lohnsteuerbescheinigung wird geprüft…'})
            # v10.4.1: Default Sonnet; local-first via AEROTAX_LSB_LOCAL_FIRST=1.
            lsb_data = _read_lsb_with_local_fallback(lsb_bytes)
        except Exception as e:
            errors.append(f'LSB: {e}')
            print(f"[hybrid] Sonnet-LSB crash: {e}")
    # v10.4.2 Memory-Release: LSB-Bytes nicht mehr gebraucht
    try:
        lsb_bytes = None
        if 'lsb' in files:
            files['lsb'] = None
    except Exception:
        pass
    gc.collect()
    _release_memory_to_os()

    # ════════════════════════════════════════════════════════════════════
    # v7.0 PIPELINE — DETERMINISTISCH
    # ════════════════════════════════════════════════════════════════════
    # 1. Sonnet liest SE strukturiert (pro Datum: stfrei/Ort/Storno)
    # 2. Sonnet liest DP strukturiert (pro Datum: marker/has_fl/overnight)
    # 3. Backend matcht beides pro Datum
    # 4. Backend klassifiziert deterministisch (SE als Anker für Z72/Z73/Z76)
    # 5. Z77 aus SE-Summen separat (für Topf-Trennung & Audit)
    # KEIN Fallback. Bei Crash → Job-Error.
    # ════════════════════════════════════════════════════════════════════

    # Schritt 2a: Sonnet-SE strukturiert (pro Zeile/Datum)
    se_structured = None
    if se_bytes:
        try:
            _heartbeat_phase(job_id, 'se_structured',
                             {'label': 'Streckeneinsatz-Abrechnung wird gelesen…'})
            se_structured = _sonnet_read_se_structured(se_bytes, year)
        except Exception as e:
            errors.append(f'SE-Structured: {type(e).__name__}: {str(e)[:200]}')
            print(f"[hybrid] Sonnet-SE-Structured crash: {type(e).__name__}: {str(e)[:200]}")
    gc.collect()
    _release_memory_to_os()

    # Schritt 2b: Sonnet-SE-Summary (für Cross-Check)
    se_summary = None
    if se_bytes:
        try:
            se_summary = _sonnet_read_se_summary_v2(se_bytes, year)
        except Exception as e:
            errors.append(f'SE-Summary: {e}')
            print(f"[hybrid] Sonnet-SE-Summary crash: {e}")
    # v10.4.2 Memory-Release: SE-Bytes nach beiden Readern nicht mehr gebraucht
    try:
        se_bytes = None
        if 'se' in files:
            files['se'] = None
    except Exception:
        pass
    gc.collect()
    _release_memory_to_os()

    # Z77 vereinheitlichen — eine Quelle für die Backend-Berechnung
    if se_structured:
        z77_from_lines = sum(
            float(s.get('stfrei_betrag', 0) or 0)
            for s in se_structured.get('se_lines', [])
            if not s.get('storno')
        )
        z77_from_months = float((se_summary or {}).get('z77_total', 0) or 0)
        z77_used = max(z77_from_lines, z77_from_months)
        diff = abs(z77_from_lines - z77_from_months)
        print(f"[v8-se] z77_from_lines={z77_from_lines:.2f} z77_from_months={z77_from_months:.2f} "
              f"z77_used={z77_used:.2f} diff={diff:.2f}")
        # v8.1.2: Diff zwischen Einzelzeilen und Summenzeilen ist ein interner Audit-
        # Datenpunkt, KEIN user-facing Fehler. Wir nehmen den höheren Wert (konservativ
        # für den User), behalten beide im se_summary für den Detailbereich.
        # se_summary auf einheitliche Z77-Quelle bringen
        if se_summary is None:
            se_summary = {}
        se_summary['z77_total'] = z77_used
        se_summary['z77_from_lines'] = z77_from_lines
        se_summary['z77_from_months'] = z77_from_months
        se_summary['z77_diff'] = round(diff, 2)
        se_summary['z77_source'] = 'einzelzeilen' if z77_from_lines >= z77_from_months else 'summenzeilen'
        se_summary.setdefault('auslandsspesen_total', sum(
            float(s.get('stfrei_betrag', 0) or 0)
            for s in se_structured.get('se_lines', [])
            if not s.get('storno') and s.get('stfrei_inland') is False
        ))
        se_summary.setdefault('inlandsspesen_total', sum(
            float(s.get('stfrei_betrag', 0) or 0)
            for s in se_structured.get('se_lines', [])
            if not s.get('storno') and s.get('stfrei_inland') is True
        ))
        se_summary.setdefault('flugmonate', sorted(set(
            int(s.get('datum', '0000-00-00')[5:7])
            for s in se_structured.get('se_lines', [])
            if s.get('datum') and not s.get('storno')
        )))

    # Schritt 3: Sonnet liest DP strukturiert
    classification = None
    structured_days = None
    document_health = None
    if dp_bytes:
        try:
            _heartbeat_phase(job_id, 'dp_start',
                             {'label': 'Flugstundenübersicht wird in Abschnitten ausgewertet…'})
            # v10.4: Chunked DP-Reader für Memory-Pressure-Reduktion auf Render Free-Tier.
            # Wenn job_id vorhanden → persistente Chunks in Supabase + Heartbeat-Tracking.
            # Sonst: fallback auf single-call.
            if job_id:
                structured_days = _sonnet_read_dp_structured_chunked_v104(
                    dp_bytes, einsatz_bytes, year, homebase, job_id=job_id)
            else:
                structured_days = _sonnet_read_dp_structured(
                    dp_bytes, einsatz_bytes, year, homebase)
            # v10.4.2 Memory-Release: DP-Bytes nach Read nicht mehr gebraucht.
            # Strukturierte Tagesdaten sind extrahiert, originale PDF-Bytes
            # können freigegeben werden bevor die Klassifikation läuft.
            try:
                dp_bytes = None
                einsatz_bytes = None
                if 'dp' in files:
                    files['dp'] = None
                if 'einsatz' in files:
                    files['einsatz'] = None
            except Exception:
                pass
            gc.collect()
            _release_memory_to_os()
            if structured_days and structured_days.get('days'):
                # Schritt 3b (v8): Document Health Check vor der Berechnung
                document_health = _document_health_check(lsb_data, se_structured, structured_days, year)
                if document_health['status'] == 'red':
                    red_reasons = '; '.join(i['reason'] for i in document_health['issues'] if i['severity'] == 'red')
                    errors.append(f'Dokumenten-Probleme: {red_reasons}')
                    print(f"[v8-health] Status RED — Berechnung gestoppt: {red_reasons}")
                else:
                    # Schritt 4: Backend matcht DP+SE pro Datum
                    matched = _match_dp_se_per_day(structured_days, se_structured, homebase)
                    print(f"[v8] Matched {len(matched)} Tage (DP+SE pro Datum)")
                    gc.collect()
                    _release_memory_to_os()
                    # Schritt 5: Deterministische Klassifikation
                    commute_min = int(form.get('anfahrt_min', 0) or 0)
                    classification = _deterministic_classify_v7(matched, year, homebase,
                                                                 commute_minutes=commute_min)
                    classification['_v7_used'] = True

                    # v8.18: Post-Classification-Health-Update — Probleme aus
                    # Klassifikation reflektieren (vma_unmapped/iata_unknown/etc.)
                    vma_unmapped_n = len(classification.get('vma_unmapped_se', []) or [])
                    iata_unknown_n = len(classification.get('iata_unknown', []) or [])
                    bmf_missing_n  = len(classification.get('bmf_missing', []) or [])
                    hotel_issues_n = len(classification.get('hotel_candidate_issues', []) or [])
                    unresolved_n   = len(classification.get('unresolved_days', []) or [])

                    if vma_unmapped_n > 0:
                        document_health.setdefault('issues', []).append({
                            'source': 'CLASSIFIER', 'severity': 'yellow',
                            'reason': f'{vma_unmapped_n} aktive SE-Zeilen ohne VMA-Klassifikation'
                        })
                        if document_health.get('status') == 'green':
                            document_health['status'] = 'yellow'
                    if iata_unknown_n > 0:
                        document_health.setdefault('issues', []).append({
                            'source': 'BMF', 'severity': 'yellow',
                            'reason': f'{iata_unknown_n} unbekannte IATA-Codes (Auslandstage könnten falsch berechnet sein)'
                        })
                        if document_health.get('status') == 'green':
                            document_health['status'] = 'yellow'
                    if bmf_missing_n > 0:
                        document_health.setdefault('issues', []).append({
                            'source': 'BMF', 'severity': 'red',
                            'reason': f'{bmf_missing_n} fehlende BMF-Mappings (aktive Auslands-SE ohne Pauschalen-Wert)'
                        })
                        document_health['status'] = 'red'
                    if hotel_issues_n > 0:
                        document_health.setdefault('issues', []).append({
                            'source': 'CLASSIFIER', 'severity': 'yellow',
                            'reason': f'{hotel_issues_n} Hotel-Candidate-Issues (overnight ohne Layover-Ort)'
                        })
                        if document_health.get('status') == 'green':
                            document_health['status'] = 'yellow'
                    if unresolved_n > 3:
                        document_health.setdefault('issues', []).append({
                            'source': 'CLASSIFIER', 'severity': 'yellow',
                            'reason': f'{unresolved_n} unresolved_days (>3) — Klassifikation unklar'
                        })
                        if document_health.get('status') == 'green':
                            document_health['status'] = 'yellow'

                    classification['_document_health'] = document_health
                    print(f"[v8-health-final] status={document_health['status']} "
                          f"vma_unmapped={vma_unmapped_n} iata_unknown={iata_unknown_n} "
                          f"bmf_missing={bmf_missing_n} hotel_issues={hotel_issues_n} "
                          f"unresolved={unresolved_n}")
            else:
                print(f"[v8] Sonnet-DP lieferte keine Tagesdaten — Job-Error")
        except Exception as e:
            etype = type(e).__name__
            import traceback as _tb
            tb_str = _tb.format_exc()
            print(f"[v8] Pipeline crash: {etype}: {str(e)[:200]} — Job-Error")
            print(f"[v8] Traceback (letzte 1000 Zeichen):\n{tb_str[-1000:]}")
            errors.append(f'Pipeline ({etype}): {str(e)[:200]}')
            classification = None
    gc.collect()
    _release_memory_to_os()

    # v7.0: KEIN Self-Reflection-Loop mehr — Klassifikation ist bereits
    # deterministisch aus DP+SE. Pendel-Risiko entfällt. Wenn Math-Inkonsistenz
    # erkannt wird (z.B. Z76 > Z77), wird das nur als Audit-Note ausgegeben,
    # nicht durch erneuten Opus-Call versucht zu beheben.
    if classification and se_summary:
        issues = _detect_classification_issues(classification, se_summary)
        if issues:
            print(f"[v8] Audit-Issues (informativ, kein Recheck): {'; '.join(issues)[:300]}")
            classification['_audit_issues'] = issues
    for _ in range(3):
        gc.collect()
    _release_memory_to_os()

    return {
        'lsb': lsb_data,
        'se_summary': se_summary,
        'classification': classification,
        'errors': errors,
    }


def _berechne_via_hybrid(form, files, job_id=None):
    """Hauptpfad: nutzt hybrid_analyze (Sonnet+Opus parallel) und baut komplettes
    Result-Dict. Liefert None wenn nicht möglich (dann fallback auf alter Code).
    job_id wird für Heartbeat-Tracking + chunked DP-Reader durchgereicht."""
    try:
        hr = hybrid_analyze(form, files, job_id=job_id)
    except Exception as e:
        print(f"[berechne-hybrid] hybrid_analyze crash: {e}")
        return None

    lst = (hr or {}).get('lsb') or {}
    cls = (hr or {}).get('classification') or {}
    se_sum = (hr or {}).get('se_summary') or {}
    errors = (hr or {}).get('errors') or []

    # Mindestanforderung: Klassifikation muss da sein (sonst keine Werbungskosten möglich)
    if not cls or cls.get('arbeitstage', 0) == 0:
        print(f"[berechne-hybrid] keine Klassifikation — fallback auf alten Code")
        return None

    notes = []
    for e in errors:
        notes.append(f'⚠ {e}')

    # ── Werte aus Hybrid-Output ──
    year_int = int(form.get('year', 2025))
    bmf_inland = BMF_INLAND_BY_YEAR.get(year_int, BMF_INLAND_BY_YEAR[2025])
    pendler = PENDLER_BY_YEAR.get(year_int, PENDLER_BY_YEAR[2025])
    reinig_satz = REINIGUNG_PRO_TAG_BY_YEAR.get(year_int, 1.60)
    trink_satz = TRINKGELD_PRO_NACHT_BY_YEAR.get(year_int, 3.60)

    # LSB-Werte
    brutto       = float(lst.get('brutto', 0) or 0)
    lohnsteuer   = float(lst.get('lohnsteuer', 0) or 0)
    soli         = float(lst.get('soli', 0) or 0)
    kirchensteuer = float(lst.get('kirchensteuer_an', 0) or 0)
    ag_z17       = float(lst.get('ag_fahrt_z17', 0) or 0)
    z18_pauschal = float(lst.get('ag_fahrt_z18_pauschal', 0) or 0)
    verpfl_z20   = float(lst.get('verpflegungszuschuss_z20', 0) or 0)
    rv_an        = float(lst.get('rv_an', 0) or 0)
    rv_ag        = float(lst.get('rv_ag', 0) or 0)
    kv_an        = float(lst.get('kv_an', 0) or 0)
    pv_an        = float(lst.get('pv_an', 0) or 0)
    av_an        = float(lst.get('av_an', 0) or 0)
    vorsorge_an  = float(lst.get('vorsorge_gesamt_an', 0) or 0)
    rv_gesamt    = float(lst.get('rv_gesamt', 0) or 0)
    identnr      = lst.get('identnr', '') or ''
    geburtsdatum = lst.get('geburtsdatum', '') or ''
    personalnummer = lst.get('personalnummer', '') or ''
    arbeitgeber  = lst.get('arbeitgeber', 'Deutsche Lufthansa AG') or 'Deutsche Lufthansa AG'
    steuerklasse = lst.get('steuerklasse', '1') or '1'
    kinderfb     = float(lst.get('kinderfreibetraege', 0) or 0)

    if not brutto:
        notes.append('⚠️ Lohnsteuerbescheinigung nicht lesbar — Brutto auf 0 gesetzt')

    # SE-Summen
    z77 = float(se_sum.get('z77_total', 0) or 0)
    spesen_gesamt = float(se_sum.get('summe_gesamt', 0) or 0)
    spesen_steuer = float(se_sum.get('summe_steuerpflichtig', 0) or 0)
    auslandsspesen_se = float(se_sum.get('auslandsspesen_total', 0) or 0)  # Z76-Anteil von Z77
    inlandsspesen_se  = float(se_sum.get('inlandsspesen_total', 0) or 0)
    flugmonate    = list(se_sum.get('flugmonate', []) or [])

    if not z77 and files.get('se'):
        notes.append('⚠️ Streckeneinsatz konnte Z77-Summe nicht ermitteln — bitte prüfen')
    elif 0 < z77 < 500:
        notes.append(f'ℹ Steuerfreie Spesen laut Streckeneinsatzabrechnung: {z77:.2f} €. '
                     f'Bei Vollzeit ungewöhnlich niedrig — bitte prüfen, ob alle Streckeneinsatz-'
                     f'Abrechnungen hochgeladen wurden.')
    elif z77 > 0:
        # v8.1.2: Positive Info-Note — der Wert wurde berücksichtigt.
        notes.append(f'ℹ Steuerfreie Spesen laut Streckeneinsatzabrechnung: {z77:.2f} €. '
                     f'Dieser Betrag wurde bei der Verrechnung berücksichtigt.')

    # Klassifikations-Werte
    arbeitstage   = int(cls.get('arbeitstage', 0) or 0)
    # v8.17 (hotfix): reinigungstage als getrennter Counter — Fallback auf
    # arbeitstage bei alten v8.16-Result-Dicts ohne reinigungstage.
    reinigungstage = int(cls.get('reinigungstage', arbeitstage) or arbeitstage)
    fahr_tage     = int(cls.get('fahr_tage', 0) or 0)
    hotel_naechte = int(cls.get('hotel_naechte', 0) or 0)
    vma_72_tage   = int(cls.get('z72_tage', 0) or 0)
    vma_73_tage   = int(cls.get('z73_tage', 0) or 0)
    vma_74_tage   = int(cls.get('z74_tage', 0) or 0)
    vma_aus       = float(cls.get('z76_eur', 0) or 0)
    nachweis_text = str(cls.get('nachweis', '') or '')
    unklare_tage  = list(cls.get('unklare_tage', []) or [])

    # ── BMF-Pauschalen × Tage (Inland) ──
    vma_72 = round(vma_72_tage * bmf_inland['tagestrip_8h'], 2)
    vma_73 = round(vma_73_tage * bmf_inland['an_abreise'], 2)
    vma_74 = round(vma_74_tage * bmf_inland['voll_24h'], 2)
    vma_in = round(vma_72 + vma_73 + vma_74, 2)

    # ── Plausi-Check (Z76 vs Auslandsspesen aus SE) ──
    # Wenn die berechneten Auslandstage stark von den steuerfrei gezahlten
    # Auslandsspesen abweichen, ist das ein Prüfhinweis — keine Fehlfunktion.
    if auslandsspesen_se > 0 and vma_aus > 0:
        ausland_diff_pct = abs(vma_aus - auslandsspesen_se) / max(auslandsspesen_se, 1)
        if ausland_diff_pct > 0.30:
            notes.append(
                f'ℹ Prüfhinweis: Berechnete VMA Ausland ({vma_aus:.2f} €) weicht spürbar '
                f'von den steuerfrei gezahlten Auslandsspesen ({auslandsspesen_se:.2f} €) ab. '
                f'Bitte vor Übernahme die VMA-Tabelle prüfen.'
            )

    # ── Fahrtkosten ──
    anreise = form.get('anreise', 'auto')
    anreise_modes = set(m.strip() for m in str(anreise).split(',') if m.strip()) or {'auto'}
    has_km = bool(anreise_modes & {'auto', 'fahrrad'})
    km = float(form.get('km', 0) or 0) if has_km else 0
    if km == 0 and cls.get('km'):
        try: km = float(cls.get('km', 0) or 0)
        except: pass

    # Jobticket-Auto-Detection aus Z18
    jobticket = 'ja_frei' if z18_pauschal > 0 else form.get('jobticket', 'nein')

    # Entfernungspauschale (verkehrsmittel-unabhängig nach § 9 EStG)
    fahr = 0.0
    fahr_breakdown = []
    f_entfernungspauschale = 0.0
    if km > 0 and fahr_tage > 0:
        f_entfernungspauschale = round(min(km, 20) * fahr_tage * pendler['lt_20km'] +
                                        max(0, km - 20) * fahr_tage * pendler['gt_21km'], 2)
        if f_entfernungspauschale > 0:
            fahr += f_entfernungspauschale
            fahr_breakdown.append(f'Entfernungspauschale ({km}km × {fahr_tage}T): {f_entfernungspauschale:.2f}€')

    # Zusätzliche selbst gezahlte Anreisekosten (separat dokumentiert für PDF)
    f_oepnv = 0.0
    oepnv_k = float(form.get('oepnv_kosten', 0) or 0)
    if oepnv_k > 0:
        f_oepnv = oepnv_k if jobticket != 'ja_frei' else 0.0
        if f_oepnv > 0:
            fahr += f_oepnv
            fahr_breakdown.append(f'ÖPNV/Bahn: {f_oepnv:.2f}€')

    f_shuttle = 0.0
    shuttle_k = float(form.get('shuttle_kosten', 0) or 0)
    if shuttle_k > 0:
        f_shuttle = shuttle_k
        fahr += f_shuttle
        fahr_breakdown.append(f'Crew-Shuttle/Zubringer: {f_shuttle:.2f}€')

    fahr = round(fahr, 2)
    if fahr_breakdown:
        print(f'[fahrtkosten] {fahr:.2f}€ aus: {", ".join(fahr_breakdown)}')

    # ── Reinigung & Trinkgeld ──
    # v8.17: Reinigung nutzt reinigungstage (Uniform-/Dienstkleidungstage),
    # nicht arbeitstage. Standby zuhause/SE-only/Mehrtages-Seminar-Folgetage
    # zählen für Reinigung NICHT (kein Uniform-Bezug an dem Tag).
    reinig = round(reinigungstage * reinig_satz, 2)
    trink  = round(hotel_naechte * trink_satz, 2)

    # ── Optionale Belege ──
    opt_keys = ['stb','gew','arb','fort','tel','konz',
                'lapt','fach','reini','bewer',
                'bu','haft','kv','rv','leb','haus','arzt','zahn','medi','pfle','under',
                'kata','spen','part','kind','hand','haed']
    opt_files = {k: files[k] for k in opt_keys if files.get(k)}
    optionale_belege = parse_optionale_belege(opt_files) if opt_files else []

    # Werbungskosten-Belege summieren (zum gesamt-Topf)
    opt_zu_gesamt = 0.0
    opt_wk_summary = []
    for b in (optionale_belege or []):
        wiso = b.get('wiso', '') or ''
        if not wiso.startswith('Werbungskosten'): continue
        betrag = float(b.get('betrag', 0) or 0)
        if b.get('key') == 'tel':
            betrag = round(betrag * 0.20, 2)
        if betrag > 0:
            opt_zu_gesamt += betrag
            opt_wk_summary.append(f"{b.get('name','?')}={betrag:.2f}€")
    opt_zu_gesamt = round(opt_zu_gesamt, 2)
    if opt_zu_gesamt > 0:
        notes.append(f'+ Werbungskosten-Belege ({", ".join(opt_wk_summary)}) +{opt_zu_gesamt:.2f}€')

    # ── Gesamt + Topf-getrennte Netto-Berechnung ──
    gesamt = round(fahr + reinig + trink + vma_in + vma_aus + opt_zu_gesamt, 2)
    vma_total = round(vma_in + vma_aus, 2)
    vma_netto = round(max(0, vma_total - z77), 2)
    fahr_netto = round(max(0, fahr - ag_z17), 2)
    netto = round(fahr_netto + reinig + trink + vma_netto + opt_zu_gesamt, 2)

    if z77 > vma_total + 5:
        notes.append(
            f'ℹ Steuerfreie Spesen wurden berücksichtigt: {z77:.2f} €. '
            f'Berechnete VMA-Pauschalen: {vma_total:.2f} €. '
            f'Da die steuerfreien Spesen die berechneten VMA-Pauschalen übersteigen, '
            f'ergibt sich für den Reisekosten-Topf kein zusätzlicher Betrag.'
        )
    if ag_z17 > fahr + 5:
        notes.append(
            f'ℹ Arbeitgeber-Fahrkostenzuschuss (Zeile 17): {ag_z17:.2f} €. '
            f'Berechnete Fahrtkosten: {fahr:.2f} €. '
            f'Da der Zuschuss die Fahrtkosten übersteigt, ergibt sich für den '
            f'Fahrtkosten-Topf kein zusätzlicher Betrag.'
        )

    # ── Plausi-Soft-Warnungen (NUR hinweisen, nichts korrigieren) ──
    if hotel_naechte > arbeitstage and arbeitstage > 0:
        notes.append(f'⚠ Plausi: Hotelnächte {hotel_naechte} > Arbeitstage {arbeitstage} — bitte prüfen')
    if fahr_tage > arbeitstage and arbeitstage > 0:
        notes.append(f'⚠ Plausi: Fahrtage {fahr_tage} > Arbeitstage {arbeitstage} — unmöglich, bitte prüfen')
    # v8.18.6: Echte unresolved_days separat von Plausi-Soft-Warnings melden
    _unresolved_count = len(cls.get('unresolved_days', []) or [])
    _plausi_count = len(cls.get('plausi_issues', []) or [])
    if _unresolved_count > 0:
        notes.append(f'ℹ {_unresolved_count} unklare Tag(e) — bitte im PDF prüfen')

    # ── Nachweis-Text (Audit-Trail) ──
    if nachweis_text:
        notes.append(f'✓ Auswertung: {nachweis_text[:500]}{"…" if len(nachweis_text) > 500 else ""}')

    # ── Notes-Deduplikation ──
    if notes:
        _seen = set()
        notes = [n for n in notes if not (n in _seen or _seen.add(n))]

    # Uploaded-Summary
    uploaded_summary = []
    not_uploaded = []
    if files.get('lsb'):  uploaded_summary.append(f"LSB ({len(files['lsb'])} Datei(en))")
    else: not_uploaded.append("Lohnsteuerbescheinigung")
    if files.get('dp'):   uploaded_summary.append(f"Flugstunden ({len(files['dp'])} Datei(en))")
    else: not_uploaded.append("Flugstunden-Übersichten")
    if files.get('se'):   uploaded_summary.append(f"Streckeneinsatz ({len(files['se'])} Datei(en))")
    else: not_uploaded.append("Streckeneinsatz-Abrechnungen")
    if files.get('einsatz'): uploaded_summary.append(f"Einsatzplan ({len(files['einsatz'])} Datei(en))")

    # ── Confidence (vereinheitlicht: KI-Hauptpfad → 90 als sane default) ──
    confidence = {
        'z77': 92, 'z76': 90, 'z72': 90, 'z73': 90,
        'fahrtage': 90, 'arbeitstage': 90, 'hotel': 90,
        'lsb': 95,
    }
    audit_source = {
        'z77':         'Sonnet liest SE-Summen',
        'z76':         'Opus klassifiziert Auslandstage aus SE',
        'z72':         'Opus klassifiziert Inland-Tagestrips (Briefingzeiten + 8h-Schwelle)',
        'z73':         'Opus klassifiziert Inland-An-/Abreise (konservativ)',
        'fahrtage':    'Opus liest Dienstplan + Einsatzplan',
        'arbeitstage': 'Opus zählt Marker Tag-für-Tag',
        'hotel':       'Opus EASA-FTL Layover-Regel ≥10h Bodenzeit',
    }
    verification_info = {
        'method': 'hybrid (Sonnet LSB+SE, Opus Klassifikation)',
        'unklare_tage_count': len(unklare_tage),
        'errors': errors,
    }

    print(f"[berechne-hybrid] FERTIG: brutto={brutto:.2f} arbeit={arbeitstage} fahr={fahr_tage} "
          f"hotel={hotel_naechte} VMA-In={vma_in:.2f} VMA-Aus={vma_aus:.2f} Z77={z77:.2f} "
          f"gesamt={gesamt:.2f} netto={netto:.2f}")
    print(f"[v8] brutto={gesamt:.2f} netto={netto:.2f} issues={len(notes)}")

    # v8.22 Step A: Recalc-State cachen für deterministische Re-Berechnung nach Review-Antworten.
    # Enthält genau die Inputs die _recompute_with_overrides braucht — KEIN Sonnet/LSB-Prompt nötig.
    _cached_recalc_state = {
        'matched_days':     locals().get('matched', []) or [],
        'year':             int(form.get('year', 2025) or 2025),
        'homebase':         str(form.get('homebase', 'FRA') or 'FRA').upper(),
        'commute_minutes':  int(form.get('anfahrt_min', 0) or 0),
        'km':               float(km),
        'fahr_oepnv':       float(f_oepnv),
        'fahr_shuttle':     float(f_shuttle),
        'ag_z17':           float(ag_z17),
        'z77':              float(z77),
        'opt_zu_gesamt':    float(opt_zu_gesamt),
    }
    return {
        'name':             form.get('name', 'Flugbegleiter'),
        'year':             form.get('year', 2025),
        '_isDemo': False,
        '_cached_recalc_state': _cached_recalc_state,
        'uploaded_summary': ', '.join(uploaded_summary),
        'not_uploaded':     ', '.join(not_uploaded) if not_uploaded else 'Alle Pflichtdokumente vorhanden',
        'notes':            notes,
        'datum':            datetime.now().strftime('%d.%m.%Y'),
        'km':               km,
        'arbeitstage':      arbeitstage,
        'reinigungstage':   reinigungstage,
        'fahr_tage':        fahr_tage,
        'hotel_naechte':    hotel_naechte,
        'vma_72_tage':      vma_72_tage,
        'vma_73_tage':      vma_73_tage,
        'vma_74_tage':      vma_74_tage,
        'vma_72':           vma_72,
        'vma_73':           vma_73,
        'vma_74':           vma_74,
        'vma_in':           vma_in,
        'vma_aus':          vma_aus,
        'fahr':             fahr,
        'fahr_entfernungspauschale': f_entfernungspauschale,
        'fahr_oepnv':       f_oepnv,
        'fahr_shuttle':     f_shuttle,
        'reinig':           reinig,
        'trink':            trink,
        'gesamt':           gesamt,
        'ag_z17':           ag_z17,
        'spesen_gesamt':    spesen_gesamt,
        'spesen_steuer':    spesen_steuer,
        'z77':              z77,
        'netto':            netto,
        'abrechnungen':     [],
        'brutto':           brutto,
        'lohnsteuer':       lohnsteuer,
        'soli':             soli,
        'kirchensteuer':    kirchensteuer,
        'steuerklasse':     steuerklasse,
        'kinderfreibetraege': kinderfb,
        'identnr':          identnr,
        'geburtsdatum':     geburtsdatum,
        'personalnummer':   personalnummer,
        'arbeitgeber':      arbeitgeber,
        'rv_an':            rv_an,
        'rv_ag':            rv_ag,
        'rv_gesamt':        rv_gesamt,
        'kv_an':            kv_an,
        'pv_an':            pv_an,
        'av_an':            av_an,
        'vorsorge_gesamt_an': vorsorge_an,
        'verpfl_z20':       verpfl_z20,
        'optionale_belege': optionale_belege,
        '_confidence':      confidence,
        '_audit_source':    audit_source,
        '_verification':    verification_info,
        '_nachweis':        nachweis_text,
        '_unklare_tage':    unklare_tage,
        '_audit_notes':     list(cls.get('audit_notes', []) or []),
        '_review_items':    _build_review_items(cls, manual_day_overrides=None),
        '_unresolved_days': list(cls.get('unresolved_days', []) or []),
        '_vma_unmapped_se': list(cls.get('vma_unmapped_se', []) or []),
        '_plausi_issues':   list(cls.get('plausi_issues', []) or []),
        '_plausi_hard_fails': list(cls.get('plausi_hard_fails', []) or []),
        '_document_health': cls.get('_document_health', {}),
        '_extra_fahrtage':         list(cls.get('extra_fahrtage', []) or []),
        '_extra_arbeitstage':      list(cls.get('extra_arbeitstage', []) or []),
        '_extra_hotelnaechte':     list(cls.get('extra_hotelnaechte', []) or []),
        '_wrong_z72_candidates':   list(cls.get('wrong_z72_candidates', []) or []),
        '_missing_z73_candidates': list(cls.get('missing_z73_candidates', []) or []),
        '_missing_z76_candidates': list(cls.get('missing_z76_candidates', []) or []),
        '_missing_deutschland_14_candidates': list(cls.get('missing_deutschland_14_candidates', []) or []),
        '_aerotax_z76_dates_amounts':   list(cls.get('aerotax_z76_dates_amounts', []) or []),
        '_rescues':                     list(cls.get('rescues', []) or []),
        '_training_sequences':          list(cls.get('training_sequences', []) or []),
        '_training_commute_candidates': list(cls.get('training_commute_candidates', []) or []),
        '_office_z72_candidates':       list(cls.get('office_z72_candidates', []) or []),
        '_office_training_time_missing_candidates': list(cls.get('office_training_time_missing_candidates', []) or []),
        '_unknown_marker_candidates':              list(cls.get('unknown_marker_candidates', []) or []),
        '_missing_reader_days':         list(cls.get('missing_reader_days', []) or []),
        '_hotel_candidate_issues':      list(cls.get('hotel_candidate_issues', []) or []),
        '_bmf_missing':            list(cls.get('bmf_missing', []) or []),
        '_iata_unknown':           list(cls.get('iata_unknown', []) or []),
        '_tage_detail':     list(cls.get('tage_detail', []) or []),
        '_klass_summary':   {
            'arbeitstage': arbeitstage, 'reinigungstage': reinigungstage,
            'fahr_tage': fahr_tage, 'hotel_naechte': hotel_naechte,
            'z72_tage': vma_72_tage, 'z73_tage': vma_73_tage, 'z74_tage': vma_74_tage,
            'z76_eur': vma_aus, 'z77_total': z77,
            'auslandsspesen_se': auslandsspesen_se, 'inlandsspesen_se': inlandsspesen_se,
        },
        # v8.1.2: Z77-Audit-Detail für aufklappbaren Detail-Bereich (intern,
        # NICHT als Top-Level-Note. Zeigt Einzelzeilen vs Summenzeilen.)
        '_z77_audit': {
            'verwendeter_wert': float(se_sum.get('z77_total', 0) or 0),
            'einzelzeilen':     float(se_sum.get('z77_from_lines', 0) or 0),
            'summenzeilen':     float(se_sum.get('z77_from_months', 0) or 0),
            'differenz':        float(se_sum.get('z77_diff', 0) or 0),
            'quelle':           se_sum.get('z77_source', '') or '',
            'auslandsspesen':   auslandsspesen_se,
            'inlandsspesen':    inlandsspesen_se,
        },
    }


def _apply_manual_day_overrides(structured_days, overrides):
    """v8.21: User-Review-Antworten als Patches auf structured_days anwenden.
    structured_days: list of day-dicts (Sonnet-Output).
    overrides: {datum: {over_8h: bool} | {start_time, end_time} | {unsure: True}}.
    Returns new list (immutable input)."""
    if not overrides:
        return structured_days
    out = []
    for d in structured_days:
        ov = overrides.get(d.get('datum')) if isinstance(d, dict) else None
        if not ov:
            out.append(d)
            continue
        d = dict(d)  # shallow copy
        if 'start_time' in ov and 'end_time' in ov:
            st, et = ov['start_time'], ov['end_time']
            try:
                sh, sm = int(st.split(':')[0]), int(st.split(':')[1])
                eh, em = int(et.split(':')[0]), int(et.split(':')[1])
                duration = (eh * 60 + em) - (sh * 60 + sm)
                if duration < 0:
                    duration += 24 * 60
                d['start_time'] = st
                d['end_time'] = et
                d['duty_duration_minutes'] = duration
                # User-Eingabe ist Tour-Abwesenheits-Zeit (Tür-zu-Tür)
                d['time_is_absence'] = True
                d['_user_review_source'] = ov.get('source', 'user_review_chatbot_time_entry')
            except (ValueError, IndexError):
                pass
        elif 'over_8h' in ov:
            if ov['over_8h']:
                d['duty_duration_minutes'] = SAME_DAY_Z72_TOTAL_MINUTES  # genau 480
                d['time_is_absence'] = True
            else:
                d['duty_duration_minutes'] = SAME_DAY_Z72_TOTAL_MINUTES - 1  # 479 = sicher unter
                d['time_is_absence'] = True
            d['_user_review_source'] = ov.get('source', 'user_review_chatbot')
        elif ov.get('unsure'):
            # User unsicher — Tag bleibt unverändert (kein Override) aber Source vermerkt
            d['_user_review_source'] = 'user_unsure'
        out.append(d)
    return out


def _build_review_items(cls, manual_day_overrides=None):
    """v8.21: Erzeugt die User-facing review_items Liste.

    Aus Klassifikator-Diagnose-Listen werden Fragen für den Chatbot abgeleitet.
    Bereits beantwortete Items werden mit status='answered' markiert.
    """
    overrides = manual_day_overrides or {}
    items = []

    # office_training_time_missing: Office/Schulung an Homebase ohne Zeitinfo
    for c in (cls.get('office_training_time_missing_candidates', []) or []):
        datum = c.get('datum', '')
        ov = overrides.get(datum)
        status = 'answered' if ov else 'pending'
        marker = c.get('marker', '') or 'Schulung/Office'
        items.append({
            'id': f'office_training_time_missing:{datum}',
            'type': 'office_training_time_missing',
            'severity': 'yellow',
            'datum': datum,
            'marker': marker,
            'activity_type': c.get('activity_type', ''),
            'question': (
                f'Am {datum} war ein Office-/Schulungstag ({marker}) eingetragen — '
                f'wir konnten keine Uhrzeit erkennen. '
                f'Warst du inklusive Hin- und Rückweg länger als 8 Stunden unterwegs?'
            ),
            'options': [
                {'value': 'yes',    'label': 'Ja, über 8h'},
                {'value': 'no',     'label': 'Nein, unter 8h'},
                {'value': 'time',   'label': 'Uhrzeit eingeben'},
                {'value': 'unsure', 'label': 'Ich weiß es nicht'},
            ],
            'money_impact_estimate': float(c.get('money_impact_estimate', 14.0)),
            'status': status,
            'user_answer': ov,
        })

    # v8.22 Now-4: unknown_marker-Items (red/yellow je nach Frequenz)
    for c in (cls.get('unknown_marker_candidates', []) or []):
        datum = c.get('datum', '')
        marker = c.get('marker', '')
        ov = overrides.get(f'_marker:{c.get("first_token","")}')
        status = 'answered' if ov else 'pending'
        items.append({
            'id': f'unknown_marker:{datum}',
            'type': 'unknown_marker',
            'severity': 'yellow',
            'datum': datum,
            'marker': marker,
            'first_token': c.get('first_token', ''),
            'question': (
                f'Am {datum} habe ich die Kennung „{marker}" gefunden, kenne sie aber noch nicht. '
                f'Was bedeutet diese Kennung?'
            ),
            'options': [
                {'value': 'flight',   'label': 'Flugdienst'},
                {'value': 'training', 'label': 'Schulung / Training'},
                {'value': 'sim',      'label': 'Simulator'},
                {'value': 'office',   'label': 'Bürodienst'},
                {'value': 'standby',  'label': 'Standby / Bereitschaft'},
                {'value': 'free',     'label': 'Frei / Urlaub / Krank'},
                {'value': 'other',    'label': 'Sonstiges'},
                {'value': 'unsure',   'label': 'Ich weiß es nicht'},
            ],
            'money_impact_estimate': 0.0,  # Marker-Klärung allein hat keinen direkten €-Impact
            'status': status,
            'user_answer': ov,
        })

    # Sortierung: pending zuerst, dann nach money_impact (absteigend), dann Datum
    items.sort(key=lambda x: (
        0 if x['status'] == 'pending' else 1,
        -float(x.get('money_impact_estimate', 0)),
        x['datum'],
    ))
    return items


# ── v8.26: Review-Gruppierung — zusammenhängende Tage clustern ──

_MARKER_FAMILY = {
    'D4':  'training',     # Schulung
    'EK':  'office',       # Bürodienst
    'SM':  'seminar',      # Seminar
    'EH':  'emergency',    # Erste-Hilfe
    'EM':  'emergency',    # Emergency Training
    'SIM': 'simulator',
}

_FAMILY_LABEL = {
    'training':  'Schulung',
    'office':    'Bürodienst',
    'seminar':   'Seminarblock',
    'emergency': 'Erste-Hilfe / Emergency',
    'simulator': 'Simulator',
    'mixed':     'Bürodienst/Schulung',
    'other':     'Schulung/Office',
}


def _marker_family(marker):
    if not marker: return 'other'
    m = str(marker).strip().upper()
    # Erste 2-3 Zeichen nehmen für Family-Match
    for key, fam in _MARKER_FAMILY.items():
        if m.startswith(key):
            return fam
    return 'other'


def _build_review_groups(review_items):
    """v8.26: Clustert review_items zu sinnvollen Gruppen für Konversation.

    Regeln:
    - office_training_time_missing-Items werden gruppiert (unknown_marker einzeln)
    - Aufeinanderfolgende Tage (≤2 Tage Abstand) mit derselben Family → Gruppe
    - Verschiedene Family in dichtem Block (z.B. D4+EK) → mixed-Gruppe
    - Übrigbleibende Einzeltage → "single_days"-Gruppe
    """
    from datetime import datetime as _dt, timedelta as _td

    pending = [it for it in (review_items or [])
               if it.get('status') == 'pending'
               and it.get('type') == 'office_training_time_missing']
    pending = sorted(pending, key=lambda x: x.get('datum', ''))

    # Datum-Parser (defensiv)
    def _parse(d):
        try: return _dt.strptime(d, '%Y-%m-%d').date()
        except Exception: return None

    # Cluster: greedy linke nach rechts, ≤2 Tage Abstand + Family-kompatibel
    groups = []
    current = None
    for it in pending:
        dt = _parse(it.get('datum'))
        fam = _marker_family(it.get('marker'))
        if current is None:
            current = {'items': [it], 'fam_set': {fam}, 'last_date': dt}
            continue
        gap = (dt - current['last_date']).days if (dt and current['last_date']) else 99
        # Family-kompatibel: gleich, oder beide ∈ {training, office} (mixed)
        fams = current['fam_set'] | {fam}
        compatible = (
            fam in current['fam_set'] or
            fams.issubset({'training', 'office'})  # D4+EK kann zu "Bürodienst/Schulung" gemixt werden
        )
        if dt and gap <= 2 and compatible:
            current['items'].append(it)
            current['fam_set'].add(fam)
            current['last_date'] = dt
        else:
            groups.append(current)
            current = {'items': [it], 'fam_set': {fam}, 'last_date': dt}
    if current is not None:
        groups.append(current)

    # Singles (Gruppen mit nur 1 Item) zusammenfassen
    multi_groups = [g for g in groups if len(g['items']) >= 2]
    singletons   = [g['items'][0] for g in groups if len(g['items']) == 1]

    out = []
    for idx, g in enumerate(multi_groups):
        items = g['items']
        first_d = items[0]['datum']
        last_d  = items[-1]['datum']
        fam_set = g['fam_set']
        if len(fam_set) == 1:
            fam = next(iter(fam_set))
            label = _FAMILY_LABEL.get(fam, _FAMILY_LABEL['other'])
            group_type = fam + '_block' if fam in ('training','seminar','emergency','office') else 'block'
        else:
            label = _FAMILY_LABEL['mixed']
            group_type = 'mixed_block'
        marker_summary = ', '.join(sorted({(it.get('marker') or '').strip() for it in items if it.get('marker')}))
        out.append({
            'group_id':       f'g{idx+1}',
            'label':          label,
            'date_range':     _format_date_range(first_d, last_d),
            'date_range_iso': [first_d, last_d],
            'marker_summary': marker_summary,
            'item_ids':       [it['id'] for it in items],
            'datums':         [it['datum'] for it in items],
            'count':          len(items),
            'group_type':     group_type,
            'suggested_question': (
                f'Für den Zeitraum {_format_date_range(first_d, last_d)} habe ich '
                f'{label}-Tage ohne Uhrzeit gefunden. Waren diese Tage jeweils '
                f'inklusive Hin- und Rückweg länger als 8 Stunden?'
            ),
        })
    if singletons:
        out.append({
            'group_id':   f'g{len(multi_groups)+1}',
            'label':      'Einzeltage',
            'date_range': _format_singletons_range(singletons),
            'date_range_iso': [singletons[0]['datum'], singletons[-1]['datum']],
            'marker_summary': ', '.join(sorted({(it.get('marker') or '').strip() for it in singletons if it.get('marker')})),
            'item_ids':   [it['id'] for it in singletons],
            'datums':     [it['datum'] for it in singletons],
            'count':      len(singletons),
            'group_type': 'single_days',
            'suggested_question': (
                f'Es gibt noch {len(singletons)} Einzeltage. Möchtest du sie zusammen '
                f'als „alle über 8h" bestätigen oder einzeln durchgehen?'
            ),
        })
    return out


def _format_date_range(first_iso, last_iso):
    """'2025-04-07' + '2025-04-11' → '07.–11.04.'."""
    try:
        a = first_iso.split('-')
        b = last_iso.split('-')
        if a == b: return f'{a[2]}.{a[1]}.'
        if a[1] == b[1] and a[0] == b[0]:
            return f'{a[2]}.–{b[2]}.{a[1]}.'
        return f'{a[2]}.{a[1]}.–{b[2]}.{b[1]}.'
    except Exception:
        return f'{first_iso} – {last_iso}'


def _format_singletons_range(items):
    if not items: return ''
    try:
        days = [it['datum'].split('-')[2]+'.'+it['datum'].split('-')[1]+'.' for it in items[:3]]
        suffix = '' if len(items) <= 3 else f' u.a.'
        return ', '.join(days) + suffix
    except Exception:
        return ''


# ── v8.26: Natural-Language-Parser für Review-Antworten ──

def _interpret_review_text(message, groups, items_by_id, _depth=0):
    """Interpretiert Freitext-Antworten in proposed_changes ohne sie anzuwenden.

    v9.0: Multi-Segment-Parser. Wenn Message durch `;` oder klare „Family + Antwort"-
    Segmente getrennt ist, splittet er und merged die Resultate.

    Returns:
        {
          'intent': str, 'proposed_changes': [...], 'confirmation_required': True,
          'summary_lines': [...], 'clarification': str|None,
          'last_bot_question': {kind, context}|None,  # v9.0 für Frontend-State-Machine
        }
    """
    import re as _re
    msg = (message or '').strip().lower()
    if not msg:
        return {'intent':'clarify', 'proposed_changes':[], 'confirmation_required':True,
                'summary_lines':[], 'clarification':'Bitte schreib mir kurz, wie du antworten möchtest.',
                'last_bot_question': None}

    # v9.0: Multi-Segment-Splitter — Eingaben mit ; oder mehrere getrennte Familie-Antwort-Pairs
    # „em ging 9-17:30; Sep 0h; Büro über 8" → 3 Segmente, jedes einzeln interpretieren
    if _depth == 0 and (';' in msg or msg.count(',') >= 2):
        segments = [s.strip() for s in _re.split(r';', msg) if s.strip()]
        # Wenn nur 1 Segment, weiter zu kommas — aber NUR splitten wenn jedes Sub-Segment einen
        # eindeutigen Family/Monats/Datums-Marker UND Antwort enthält
        if len(segments) == 1:
            cands = [s.strip() for s in _re.split(r',\s*(?=[a-zäöü]{3})', msg) if s.strip()]
            # Nur splitten wenn ≥2 Sub-Segmente jeweils ein klares Family/Monats-Schlüsselwort haben
            FAM_RE = _re.compile(r'\b(d4|ek|sm|eh|em|seminar|schulung|bürodienst|buerodienst|büro|buero|office|emergency|jan|feb|m[aä]r|apr|mai|jun|jul|aug|sep|okt|nov|dez|januar|februar|m[aä]rz|april|juni|juli|august|september|oktober|november|dezember)\b')
            recognised = sum(1 for s in cands if FAM_RE.search(s))
            if recognised >= 2:
                segments = cands
        if len(segments) >= 2:
            all_changes = []
            for seg in segments:
                sub = _interpret_review_text(seg, groups, items_by_id, _depth=_depth+1)
                for c in (sub.get('proposed_changes') or []):
                    if not any(x['review_item_id']==c['review_item_id'] for x in all_changes):
                        all_changes.append(c)
            if all_changes:
                return _build_proposed('multi_segment', all_changes, items_by_id,
                                        summary='Mehrere Antworten zusammengeführt')

    all_pending_ids = []
    for g in groups:
        for iid in g.get('item_ids', []):
            it = items_by_id.get(iid)
            if it and it.get('status') == 'pending':
                all_pending_ids.append(iid)

    # ── v8.38: „alle X AUSSER Y" Pattern zuerst (sonst hijackt es bulk_yes)
    aua_match = _re.search(r'\balle\b.*?\baußer\b\s*(?P<exc>.+)', msg)
    if aua_match:
        head = msg[:aua_match.start('exc')]  # alles vor exc, inkl. „außer"
        if _re.search(r'(über\s*8|über\s*acht|>\s*8|länger|mehr\s*als\s*8|\bja\b)', head):
            main_ans = 'yes'
        elif _re.search(r'(unter\s*8|unter\s*acht|<\s*8|kürzer|weniger\s*als\s*8|\b0\s*h?\b|\bnull\b|\bnein\b)', head):
            main_ans = 'no'
        else:
            main_ans = 'yes'
        exc = aua_match.group('exc').lower()
        # Exception identifizieren: Datum-Liste, Family-Keyword, Gruppen-Label
        exc_ids = set()
        # Datum-Pattern in der Exception
        for dm in _re.finditer(r'\b(\d{1,2})\.\s*(\d{1,2})?\.?', exc):
            d, mo = int(dm.group(1)), (int(dm.group(2)) if dm.group(2) else None)
            for iid in all_pending_ids:
                it = items_by_id.get(iid)
                if not it: continue
                try:
                    _, mm, dd = (it.get('datum') or '').split('-')
                    mm, dd = int(mm), int(dd)
                except Exception: continue
                if mo and mm != mo: continue
                if dd == d: exc_ids.add(iid)
        # Family- / Gruppen-Keyword in Exception
        ex_kw_map = {
            'einzeltag': 'single_days', 'einzelne': 'single_days',
            'seminar': 'seminar', 'sm': 'seminar',
            'schulung': 'training', 'd4': 'training',
            'bürodienst': 'office', 'buerodienst': 'office', 'büro': 'office', 'buero': 'office', 'ek': 'office',
            'emergency': 'emergency', 'erste hilfe': 'emergency', 'erste-hilfe': 'emergency', 'eh': 'emergency', 'em': 'emergency',
        }
        for kw, target in ex_kw_map.items():
            if kw in exc:
                # Items deren Family/group_type matched
                for g in groups:
                    gtype = g.get('group_type', '')
                    glab  = (g.get('label') or '').lower()
                    if (target == 'single_days' and gtype == 'single_days') or \
                       (target in gtype) or \
                       (target in glab):
                        for iid in g.get('item_ids', []):
                            if iid in items_by_id and items_by_id[iid].get('status') == 'pending':
                                exc_ids.add(iid)
        # Monatsname in Exception
        month_map_x = {'januar':1,'februar':2,'märz':3,'maerz':3,'april':4,'mai':5,'juni':6,'juli':7,
                       'august':8,'september':9,'oktober':10,'november':11,'dezember':12}
        for mn, mnum in month_map_x.items():
            if mn in exc:
                for iid in all_pending_ids:
                    it = items_by_id.get(iid)
                    if not it: continue
                    try:
                        _, mm, _ = (it.get('datum') or '').split('-')
                        if int(mm) == mnum: exc_ids.add(iid)
                    except Exception: continue
        # proposed: alle pending außer exc_ids
        changes = [{'review_item_id': iid, 'answer': main_ans}
                   for iid in all_pending_ids if iid not in exc_ids]
        if not changes:
            return {
                'intent': 'clarify',
                'proposed_changes': [],
                'confirmation_required': True,
                'summary_lines': [],
                'clarification': 'Ich habe „alle außer …" verstanden, aber keine passenden Tage gefunden. Magst du es anders formulieren?',
            }
        excl_count = len(exc_ids)
        excl_hint = (' (' + str(excl_count) + ' Tag' + ('' if excl_count==1 else 'e') + ' bleiben offen)') if excl_count else ''
        summary_label = ('Alle über 8h' if main_ans=='yes' else 'Alle unter 8h' if main_ans=='no' else 'Alle unsicher') + ' — außer den Ausnahmen' + excl_hint
        return _build_proposed('bulk_all_except', changes, items_by_id, summary=summary_label)

    # ── Bulk: "alle ja" / "alle über 8h" / "alle nein" / "weiß nicht"
    # v8.35: 0/0h/null = no-Äquivalent
    bulk_yes = bool(_re.search(r'\balle\b[^.,]*(ja|über\s*8|>\s*8|länger|mehr\s*als\s*8)', msg))
    bulk_no  = bool(_re.search(r'\balle\b[^.,]*(nein|unter\s*8|<\s*8|kürzer|weniger\s*als\s*8|\b0\b|\bnull\b|\b0\s*h\b|\b0\s*stunden\b)', msg))
    bulk_unsure = bool(_re.search(r'\b(weiß|weiss).*nicht\b|\bunsicher\b|\bkeine\s*ahnung\b', msg))
    if bulk_yes and not bulk_no:
        changes = [{'review_item_id': iid, 'answer': 'yes'} for iid in all_pending_ids]
        return _build_proposed('bulk_all', changes, items_by_id, summary='Alle als über 8h bestätigen')
    if bulk_no and not bulk_yes:
        changes = [{'review_item_id': iid, 'answer': 'no'} for iid in all_pending_ids]
        return _build_proposed('bulk_all', changes, items_by_id, summary='Alle als unter 8h markieren')
    if bulk_unsure and not bulk_yes and not bulk_no:
        changes = [{'review_item_id': iid, 'answer': 'unsure'} for iid in all_pending_ids]
        return _build_proposed('bulk_all', changes, items_by_id, summary='Alle als unsicher markieren')

    # ── Datum-spezifisch: "08.04 nein, 09-11 ja" / "07.04 ja, 08.04 nein, 09-11 ja"
    changes = []
    summary_lines = []
    matched_any = False
    # Pattern: Datum oder Datumsbereich + ja/nein
    # 08.04, 8.4., 24.-29.04, 24-29.04, 09–11.04
    date_pat = r'(\d{1,2})\s*(?:[\.\-–]\s*(\d{1,2}))?\s*\.?\s*(?:(\d{1,2})\.?)?'
    seg_pat  = _re.compile(
        r'(?P<d1>\d{1,2})\s*\.\s*(?P<m1>\d{1,2})?\.?'                            # 08.04 / 08.
        r'(?:\s*[\-–]\s*(?P<d2>\d{1,2})\s*(?:\.\s*(?P<m2>\d{1,2})?\.?)?)?'      # –11.04 optional
        r'\s*(?P<ans>ja|nein|über\s*8|unter\s*8|>\s*8|<\s*8|unsicher|weiß\s*nicht)',
        _re.IGNORECASE
    )
    for m in seg_pat.finditer(msg):
        d1 = int(m.group('d1'))
        d2 = int(m.group('d2')) if m.group('d2') else d1
        mo1 = int(m.group('m1')) if m.group('m1') else None
        mo2 = int(m.group('m2')) if m.group('m2') else mo1
        ans_raw = m.group('ans').lower()
        if 'unsicher' in ans_raw or 'weiß' in ans_raw or 'weiss' in ans_raw:
            ans = 'unsure'
        elif 'nein' in ans_raw or 'unter' in ans_raw or '<' in ans_raw:
            ans = 'no'
        else:
            ans = 'yes'
        # Datums in Range matchen mit pending Items
        for iid in all_pending_ids:
            it = items_by_id.get(iid)
            if not it: continue
            try:
                _, mm, dd = (it.get('datum') or '').split('-')
                mm, dd = int(mm), int(dd)
            except Exception:
                continue
            if mo1 and mm != mo1: continue
            if dd < d1 or dd > d2: continue
            if any(c['review_item_id']==iid for c in changes): continue
            changes.append({'review_item_id': iid, 'answer': ans})
            matched_any = True

    # ── "Rest ja" / "beim Rest 0" / "andere nein" / "übrige unsicher"
    # v8.35: 0/0h/null als no-Äquivalent erkennen
    rest_match = _re.search(
        r'(?:rest|andere|übrige|sonst|sonstigen)\s*(ja|nein|unsicher|über\s*8|unter\s*8|0\b|null\b|0\s*h\b|0\s*stunden)',
        msg
    )
    if rest_match:
        ans_raw = rest_match.group(1).lower()
        if 'unsicher' in ans_raw:
            ans = 'unsure'
        elif ('ja' in ans_raw) or ('über' in ans_raw):
            ans = 'yes'
        elif ('nein' in ans_raw) or ('unter' in ans_raw) or ('0' in ans_raw) or ('null' in ans_raw):
            ans = 'no'
        else:
            ans = 'unsure'
        already = {c['review_item_id'] for c in changes}
        for iid in all_pending_ids:
            if iid in already: continue
            changes.append({'review_item_id': iid, 'answer': ans})
        matched_any = True

    # ── Group-Label-spezifisch: "April ja", "September nein", "Seminar ja"
    month_map = {'januar':1,'februar':2,'märz':3,'maerz':3,'april':4,'mai':5,'juni':6,'juli':7,
                 'august':8,'september':9,'oktober':10,'november':11,'dezember':12}
    fam_keywords = {
        'training': ['schulung','training','d4'],
        'office':   ['bürodienst','buerodienst','büro','buero','office','ek'],
        'seminar':  ['seminar','sm'],
        'emergency':['emergency','erste hilfe','erste-hilfe','eh','em'],
    }
    # v9.0: Auch kurze Monatsnamen (Sep/Jan/...) + 0/0h/null als no-Äquivalent
    label_segs = _re.findall(
        r'(januar|februar|m[aä]rz|april|mai|juni|juli|august|september|oktober|november|dezember|'
        r'\bjan|\bfeb|\bm[aä]r|\bapr|\bjun|\bjul|\baug|\bsep|\bokt|\bnov|\bdez|'
        r'schulung|training|d4|bürodienst|buerodienst|büro|buero|office|ek|seminar|sm|'
        r'emergency|erste\s*hilfe|erste-hilfe|eh|em)\s*(?:waren|war|wurde|sind|ist)?\s*'
        r'(ja|nein|unsicher|weiß\s*nicht|weiss\s*nicht|über\s*8|über\s*acht|unter\s*8|unter\s*acht|0\s*h?\b|null)',
        msg
    )
    short_month_map = {
        'jan':1, 'feb':2, 'mär':3, 'maer':3, 'mar':3, 'apr':4, 'mai':5, 'jun':6,
        'jul':7, 'aug':8, 'sep':9, 'okt':10, 'nov':11, 'dez':12,
    }
    for kw, ans_raw in label_segs:
        kw = kw.lower().strip()
        if 'unsicher' in ans_raw or 'weiß' in ans_raw or 'weiss' in ans_raw:
            ans = 'unsure'
        elif 'nein' in ans_raw or 'unter' in ans_raw or '0' in ans_raw or 'null' in ans_raw:
            ans = 'no'
        else:
            ans = 'yes'
        # Monat? (lang oder kurz)
        target_month = month_map.get(kw) or short_month_map.get(kw[:3])
        target_fam = None
        if not target_month:
            for fam, kws in fam_keywords.items():
                if any(k in kw for k in kws):
                    target_fam = fam
                    break
        already = {c['review_item_id'] for c in changes}
        for iid in all_pending_ids:
            if iid in already: continue
            it = items_by_id.get(iid)
            if not it: continue
            try:
                _, mm, _ = (it.get('datum') or '').split('-')
                mm = int(mm)
            except Exception: continue
            fam = _marker_family(it.get('marker'))
            if target_month and mm != target_month: continue
            if target_fam and fam != target_fam: continue
            changes.append({'review_item_id': iid, 'answer': ans})
            matched_any = True

    if matched_any and changes:
        return _build_proposed('mixed' if len(label_segs)+len(seg_pat.findall(msg)) > 1 else 'group_answer',
                               changes, items_by_id)

    # Nichts erkannt
    return {
        'intent': 'clarify',
        'proposed_changes': [],
        'confirmation_required': True,
        'summary_lines': [],
        'clarification': (
            'Ich bin mir nicht sicher, was du meinst. Du kannst z.B. schreiben:\n'
            '• "alle über 8h"\n'
            '• "April ja, September nein"\n'
            '• "07.04 ja, 08.04 nein, 09–11 ja"\n'
            '• "weiß ich nicht"'
        ),
    }


def _build_proposed(intent, changes, items_by_id, summary=None):
    """Helper: baut proposed-changes-Response mit Summary-Lines."""
    lines = []
    # Gruppieren der Changes nach Datum für saubere Summary
    yes_dates = sorted([items_by_id[c['review_item_id']]['datum']
                        for c in changes if c['answer']=='yes' and c['review_item_id'] in items_by_id])
    no_dates  = sorted([items_by_id[c['review_item_id']]['datum']
                        for c in changes if c['answer']=='no' and c['review_item_id'] in items_by_id])
    uns_dates = sorted([items_by_id[c['review_item_id']]['datum']
                        for c in changes if c['answer']=='unsure' and c['review_item_id'] in items_by_id])
    if yes_dates: lines.append(f'• {len(yes_dates)} Tag(e) als ÜBER 8h: ' + ', '.join(yes_dates[:6]) + ('…' if len(yes_dates)>6 else ''))
    if no_dates:  lines.append(f'• {len(no_dates)} Tag(e) als UNTER 8h: ' + ', '.join(no_dates[:6]) + ('…' if len(no_dates)>6 else ''))
    if uns_dates: lines.append(f'• {len(uns_dates)} Tag(e) als UNSICHER: ' + ', '.join(uns_dates[:6]) + ('…' if len(uns_dates)>6 else ''))
    return {
        'intent': intent,
        'proposed_changes': changes,
        'confirmation_required': True,
        'summary_lines': lines,
        'summary_header': summary or 'Ich habe verstanden:',
        'clarification': None,
    }


def berechne(form, files, job_id=None):
    """v8 Berechnungs-Einstieg: Sonnet-Reader → Backend-Klassifikator.

    Bei Pipeline-Fehler: Exception mit nutzerfreundlicher Meldung.
    Kein automatischer Wiederholungs-Versuch — der User entscheidet.
    """
    hybrid_result = _berechne_via_hybrid(form, files, job_id=job_id)
    if hybrid_result is not None:
        return hybrid_result

    raise RuntimeError(
        "Die Auswertung konnte nicht abgeschlossen werden. Deine Sitzung bleibt gültig — "
        "bitte versuche es erneut oder kontaktiere den Support."
    )

    # ── DEAD CODE BELOW (alter Multi-Parser-Pfad bleibt im File für historische
    # Referenz, wird aber nicht mehr aufgerufen) ──
    # Falls jemand das wieder aktivieren will: oben den raise entfernen.
    print(f"[berechne] FALLBACK: alter Multi-Parser-Code wird verwendet")
    notes = []  # Hinweise über geschätzte Werte
    available_texts = {}  # Raw texts for inference
    missing = []  # Was fehlt oder fehlschlug

    # ── LOHNSTEUERBESCHEINIGUNG ────────────────────────────────
    lst = None
    if files.get('lsb'):
        lst = parse_lohnsteuerbescheinigung(files['lsb'])
        # Collect raw text for inference fallback
        for item in files['lsb']:
            pdf_bytes = item[0] if isinstance(item, tuple) else item
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    available_texts['lsb_text'] = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                    break
            except: pass
        if not lst or not lst.get('brutto'):
            missing.append('Lohnsteuerbescheinigung (nicht lesbar)')
            lst = None
        gc.collect()  # LSB-PDF-Buffer freigeben
    else:
        missing.append('Lohnsteuerbescheinigung (nicht hochgeladen)')

    # ── EINSATZPLAN (CAS) — zuerst parsen, damit SE-Warnings ihn berücksichtigen ──
    einsatz_data = None
    if files.get('einsatz'):
        try:
            einsatz_data = parse_einsatzplan_mit_ki(files['einsatz'], year=int(form.get('year', 2025)))
            if einsatz_data:
                print(f"Einsatzplan: {einsatz_data.get('monate_geparst',0)}/12 Monate, "
                      f"{len(einsatz_data.get('umlaeufe',[]))} Umläufe, "
                      f"Spesen-Total={einsatz_data.get('spesen_total',0):.2f} EUR, "
                      f"Tagestrips={einsatz_data.get('tagestrips_count',0)}")
        except Exception as e:
            print(f"Einsatzplan-Parse fail: {e}")
            einsatz_data = None
        gc.collect()  # Einsatzplan-PDFs Buffer freigeben

    # Helper: Einsatzplan-Monate auswerten (welche Monate flugfrei?)
    _MONAT_MAP = {'JAN':1,'FEB':2,'MAR':3,'MÄR':3,'APR':4,'MAI':5,'JUN':6,
                  'JUL':7,'AUG':8,'SEP':9,'OKT':10,'NOV':11,'DEZ':12}
    einsatz_flugmonate = set()       # Monate mit ≥1 Umlauf
    einsatz_abgedeckte_monate = set() # alle Monate die im Einsatzplan auftauchen
    if einsatz_data:
        for ml in einsatz_data.get('monatslisten', []):
            mn_part = (ml or '').split()[0].upper() if ml else ''
            mn = _MONAT_MAP.get(mn_part)
            if mn:
                einsatz_abgedeckte_monate.add(mn)
        for u in einsatz_data.get('umlaeufe', []):
            mn_part = (u.get('monat', '') or '').split()[0].upper()
            mn = _MONAT_MAP.get(mn_part)
            if mn:
                einsatz_flugmonate.add(mn)
    einsatz_flugfreie_monate = einsatz_abgedeckte_monate - einsatz_flugmonate

    # ── STRECKENEINSATZ-ABRECHNUNGEN ──────────────────────────
    se_data = None
    if files.get('se'):
        # Collect raw text
        se_texts = []
        for item in files['se']:
            pdf_bytes = item[0] if isinstance(item, tuple) else item
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    se_texts.append('\n'.join(p.extract_text() or '' for p in pdf.pages))
            except: pass
        if se_texts:
            available_texts['se_text'] = '\n'.join(se_texts)
        
        se_data = parse_streckeneinsatz_mit_ki(files['se'], year=int(form.get('year', 2025)))
        if not se_data or not se_data.get('summe_gesamt', 0) > 0:
            missing.append('Streckeneinsatz-Abrechnungen (nicht vollständig lesbar)')
            se_data = None
        else:
            # Welche Monate sind tatsächlich durch Flugzeilen abgedeckt?
            # (Eine SE kann Flüge aus 2 Monaten enthalten, daher Flugdaten-basiert prüfen)
            se_flugmonate = set(se_data.get('flugmonate', []))
            month_names = ['Jan','Feb','Mär','Apr','Mai','Jun','Jul','Aug','Sep','Okt','Nov','Dez']

            # Welche Monate sollten Flüge haben? Primär: laut Einsatzplan.
            # Ohne Einsatzplan: alle 12 Monate annehmen (Vollzeit) — sonst keine Warnung möglich.
            if einsatz_data:
                # Flugmonate laut Einsatzplan = die Soll-Vorgabe
                expected_flugmonate = set(einsatz_flugmonate)
                fehlende_flugmonate = expected_flugmonate - se_flugmonate
                if fehlende_flugmonate:
                    abr_list = se_data.get('abrechnungen', [])
                    avg_steuerfrei = sum(a.get('steuerfrei', 0) for a in abr_list) / max(1, len(abr_list))
                    fehlend_str = ', '.join(month_names[m-1] for m in sorted(fehlende_flugmonate))
                    est_missing = round(avg_steuerfrei * len(fehlende_flugmonate), 2)
                    notes.append(
                        f'⚠️ Streckeneinsatz unvollständig: laut Einsatzplan wurde in {fehlend_str} geflogen, '
                        f'die SE-Abrechnungen decken diese Monate aber nicht ab. '
                        f'Geschätzter Verlust: ~{est_missing:.0f}€ steuerfreie Spesen. '
                        f'Tipp: lade die fehlenden Abrechnungen nach via "Mit Code anpassen".'
                    )
                elif einsatz_flugfreie_monate:
                    flugfrei_strs = [month_names[m-1] for m in sorted(einsatz_flugfreie_monate)]
                    notes.append(
                        f'ℹ {len(se_flugmonate)} Flugmonate aus SE erfasst — '
                        f'flugfreie Monate ({", ".join(flugfrei_strs)}) laut Einsatzplan benötigen keine Abrechnung.'
                    )
            else:
                # Kein Einsatzplan → einfach neutral hinweisen welche Monate keine Flüge zeigen
                # (kann Teilzeit/Urlaub sein oder fehlende Abrechnung — nicht entscheidbar)
                if 0 < len(se_flugmonate) < 12:
                    fehlend = [m for m in range(1,13) if m not in se_flugmonate]
                    fehlend_str = ', '.join(month_names[m-1] for m in fehlend)
                    notes.append(
                        f'ℹ Streckeneinsatz: in {fehlend_str} wurde kein Flug erkannt. '
                        f'Falls Teilzeit/Urlaub/Frei: alles gut. Falls eine Abrechnung fehlt: '
                        f'lade sie nach via "Mit Code anpassen" oder lade den Einsatzplan hoch — '
                        f'dann erkennen wir flugfreie Monate automatisch.'
                    )
    else:
        missing.append('Streckeneinsatz-Abrechnungen (nicht hochgeladen)')
    gc.collect()  # SE-PDFs + Claude-Validation-Buffer freigeben

    # ── FLUGSTUNDEN-ÜBERSICHTEN ───────────────────────────────
    dp = None
    if files.get('dp'):
        # Collect raw text
        dp_texts = []
        for item in files['dp']:
            pdf_bytes = item[0] if isinstance(item, tuple) else item
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    dp_texts.append('\n'.join(p.extract_text() or '' for p in pdf.pages))
            except: pass
        if dp_texts:
            available_texts['dp_text'] = '\n'.join(dp_texts)
        
        try:
            anreise_form = form.get('anreise', 'auto')
            km_form_val = float(form.get('km', 0) or 0) if anreise_form in ('auto', 'fahrrad') else 0
            homebase_iata = _extract_homebase(form.get('base', 'Frankfurt (FRA)'))
            print(f"Homebase erkannt: {homebase_iata} (aus Form: '{form.get('base','')}')")
            dp = parse_dienstplan_mit_ki(files['dp'], se_bytes_list=files.get('se'), km_form=km_form_val,
                                          se_hints=se_data if se_data else None, homebase=homebase_iata,
                                          einsatzplan_bytes_list=files.get('einsatz'))
        except RuntimeError as e:
            raise
        if not dp or not dp.get('arbeitstage'):
            missing.append('Flugstunden-Übersichten (Analyse fehlgeschlagen — bitte nochmal versuchen)')
            dp = None
        gc.collect()  # DP-PDFs Buffer + Sonnet-Response freigeben
    else:
        missing.append('Flugstunden-Übersichten (nicht hochgeladen)')

    # ── SMART INFERENCE für fehlende Daten ────────────────────
    inferred = {}
    if missing and available_texts:
        # Bereits geparste Werte zusammenfassen, damit Inferenz NICHT darüber editorialisiert
        parsed_summary = {}
        if lst:
            parsed_summary['LSB_brutto'] = lst.get('brutto', 0)
            parsed_summary['LSB_lohnsteuer'] = lst.get('lohnsteuer', 0)
            parsed_summary['LSB_z17'] = lst.get('ag_fahrt_z17', 0)
        if se_data:
            parsed_summary['SE_monate_geparst'] = len(se_data.get('abrechnungen', []))
            parsed_summary['SE_summe_gesamt'] = se_data.get('summe_gesamt', 0)
            parsed_summary['SE_summe_steuerfrei_z77'] = se_data.get('summe_steuerfrei', 0)
        if dp:
            parsed_summary['DP_arbeitstage'] = dp.get('arbeitstage', 0)
            parsed_summary['DP_fahr_tage'] = dp.get('fahr_tage', 0)
            parsed_summary['DP_hotel_naechte'] = dp.get('hotel_naechte', 0)
        # Ausfallzeit-Hint für Inferenz (sonst wird Vollzeit angenommen)
        ausfall_for_inf = int(form.get('ausfallzeit_monate', 0) or 0)
        if ausfall_for_inf > 0:
            parsed_summary['ausfallzeit_monate'] = ausfall_for_inf
            parsed_summary['_hint'] = (
                f"User hat {ausfall_for_inf} Monat(e) Ausfallzeit (Mutterschutz/Krank/Teilzeit) angegeben. "
                f"Werte entsprechend skalieren — nicht von Vollzeit ausgehen."
            )
        print(f"Running inference for missing: {missing}; parsed_summary={parsed_summary}")
        inferred, inf_notes = infer_missing_data_with_ki(files, available_texts, missing, parsed_summary=parsed_summary)
        notes.extend(inf_notes)

    # ── WERTE ZUSAMMENFÜHREN (real > inferred > default 0) ────
    def get(key, real_val, default=0):
        if real_val is not None and real_val != 0:
            return real_val
        if key in inferred:
            return inferred[key]
        return default

    # LSB values
    if lst:
        ag_z17          = lst.get('ag_fahrt_z17', 0)
        brutto          = lst.get('brutto', 0)
        lohnsteuer      = lst.get('lohnsteuer', 0)
        soli            = lst.get('soli', 0)
        kirchensteuer   = lst.get('kirchensteuer_an', 0)
        arbeitgeber     = lst.get('arbeitgeber', 'Deutsche Lufthansa AG')
        rv_an           = lst.get('rv_an', 0)
        rv_ag           = lst.get('rv_ag', 0)
        kv_an           = lst.get('kv_an', 0)
        pv_an           = lst.get('pv_an', 0)
        av_an           = lst.get('av_an', 0)
        vorsorge_an     = lst.get('vorsorge_gesamt_an', 0)
        rv_gesamt       = lst.get('rv_gesamt', 0)
        steuerklasse    = lst.get('steuerklasse', '1')
        kinderfb        = lst.get('kinderfreibetraege', 0)
        identnr         = lst.get('identnr', '')
        geburtsdatum    = lst.get('geburtsdatum', '')
        personalnummer  = lst.get('personalnummer', '')
        verpfl_z20      = lst.get('verpflegungszuschuss_z20', 0)
    else:
        ag_z17 = inferred.get('ag_z17', 0)
        brutto = inferred.get('brutto', 0)
        lohnsteuer = inferred.get('lohnsteuer', 0)
        soli = kirchensteuer = 0
        arbeitgeber = 'Deutsche Lufthansa AG'
        rv_an = rv_ag = kv_an = pv_an = av_an = 0
        vorsorge_an = rv_gesamt = 0
        steuerklasse = '1'; kinderfb = 0
        identnr = geburtsdatum = personalnummer = ''
        verpfl_z20 = 0
        if not ag_z17 and not brutto:
            notes.append('⚠️ Lohnsteuerbescheinigung fehlt — Z17-Abzug und Bruttolohn auf 0 gesetzt.')

    # Streckeneinsatz values
    if se_data:
        abrechnungen  = se_data.get('abrechnungen', [])
        spesen_gesamt = se_data.get('summe_gesamt', 0)
        spesen_steuer = se_data.get('summe_steuerpflichtig', 0)
        z77           = se_data.get('summe_steuerfrei', spesen_gesamt - spesen_steuer)
    else:
        abrechnungen  = inferred.get('abrechnungen', [])
        spesen_gesamt = inferred.get('spesen_gesamt', 0)
        spesen_steuer = inferred.get('spesen_steuer', 0)
        z77           = inferred.get('z77', 0)
        if not spesen_gesamt:
            notes.append('⚠️ Streckeneinsatz-Abrechnungen fehlen — Z77-Abzug konnte nicht berechnet werden.')

    # Dienstplan values (Arbeitstage/Hotel/Fahrtage kommen aus DP/Claude)
    if dp:
        arbeitstage    = dp.get('arbeitstage', 0)
        fahr_tage      = dp.get('fahr_tage', 0)
        hotel_naechte  = dp.get('hotel_naechte', 0)
        ausland_touren = dp.get('ausland_touren', [])
        km_dp          = dp.get('km', 0)
    else:
        arbeitstage    = inferred.get('arbeitstage', 0)
        fahr_tage      = inferred.get('fahr_tage', 0)
        hotel_naechte  = inferred.get('hotel_naechte', 0)
        ausland_touren = []
        km_dp          = inferred.get('km', 0)
        if not arbeitstage:
            notes.append('⚠️ Flugstunden-Übersichten fehlen — Arbeitstage und VMA konnten nicht berechnet werden.')

    # ── VMA-KATEGORISIERUNG ─────────────────────────────────────
    # SE-Parser liest deterministisch (literal-Werte aus dem Dokument). Claude greift nur bei
    # unklaren Zeilen ein. Wenn SE komplett sauber gelesen wurde → SE-Werte sind authoritativ.
    se_unklar = len((se_data or {}).get('unklare_zeilen', []))
    if se_data and se_unklar == 0:
        # 100% deterministisch gelesen, keine Edge-Cases
        vma_72_tage = se_data.get('z72_tage', 0)
        vma_73_tage = se_data.get('z73_tage', 0)
        vma_74_tage = se_data.get('z74_tage', 0)
        vma_72 = se_data.get('z72_eur', vma_72_tage * 14)
        vma_73 = se_data.get('z73_eur', vma_73_tage * 14)
        vma_74 = se_data.get('z74_eur', vma_74_tage * 28)
        vma_aus_se_det = se_data.get('z76_eur', 0)
        print(f"VMA aus SE deterministisch (sauber): Z72={vma_72_tage}T/{vma_72}€  Z73={vma_73_tage}T/{vma_73}€  Z74={vma_74_tage}T/{vma_74}€  Z76={vma_aus_se_det}€")
    elif se_data:
        # Teilweise deterministisch — Claude hat unklare Zeilen ergänzt
        vma_72_tage = max(se_data.get('z72_tage', 0), (dp or {}).get('vma_72_tage', 0))
        vma_73_tage = max(se_data.get('z73_tage', 0), (dp or {}).get('vma_73_tage', 0))
        vma_74_tage = max(se_data.get('z74_tage', 0), (dp or {}).get('vma_74_tage', 0))
        vma_72 = se_data.get('z72_eur', vma_72_tage * 14) or vma_72_tage * 14
        vma_73 = se_data.get('z73_eur', vma_73_tage * 14) or vma_73_tage * 14
        vma_74 = se_data.get('z74_eur', vma_74_tage * 28) or vma_74_tage * 28
        vma_aus_se_det = se_data.get('z76_eur', 0)
        # Warnung nur wenn wirklich auffällig viele unklare Zeilen (>30 absolut)
        # Bis dahin hat Claude+Opus das ohnehin sauber gehandelt — kein User-Stress.
        if se_unklar > 30:
            notes.append(f'⚠ {se_unklar} SE-Zeilen waren nicht eindeutig lesbar — Werte ggf. ungenau, bitte prüfen.')
        print(f"VMA hybrid (SE-Parser + Claude für {se_unklar} unklare): Z72={vma_72_tage}T  Z73={vma_73_tage}T  Z76={vma_aus_se_det}€")
    else:
        # Kein SE-Parser-Output → komplett auf DP/Claude angewiesen
        vma_72_tage = (dp or inferred).get('vma_72_tage', 0)
        vma_73_tage = (dp or inferred).get('vma_73_tage', 0)
        vma_74_tage = (dp or inferred).get('vma_74_tage', 0)
        vma_72 = vma_72_tage * 14
        vma_73 = vma_73_tage * 14
        vma_74 = vma_74_tage * 28
        vma_aus_se_det = 0

    # ── KM: form > dienstplan > inferred ──────────────────────
    anreise = form.get('anreise', 'auto')
    # anreise kann CSV sein (z.B. "auto,shuttle"). km nur relevant wenn auto/fahrrad in den Modi.
    _anreise_modes_for_km = set(m.strip() for m in str(anreise).split(',') if m.strip())
    km = float(form.get('km', 0)) if (_anreise_modes_for_km & {'auto', 'fahrrad'}) else 0
    if km == 0 and km_dp > 0:
        km = km_dp

    # ── JAHR-SPEZIFISCHE PAUSCHALEN (zuerst, weil VMA-Lookup year_int braucht) ──
    year_int = int(form.get('year', 2025))
    bmf_inland = BMF_INLAND_BY_YEAR.get(year_int, BMF_INLAND_BY_YEAR[2025])

    # ── VMA BERECHNEN ─────────────────────────────────────────
    vma_in = vma_72 + vma_73 + vma_74

    # VMA Ausland — Priorität: SE deterministisch > DP/Claude > inferred
    if vma_aus_se_det > 0:
        vma_aus = vma_aus_se_det
    elif dp and ausland_touren:
        vma_aus = 0
        for t in ausland_touren:
            ort = t.get('ort', '').upper()
            v = bmf_lookup(ort, year_int)
            if v:
                s24, sab = v
                vma_aus += t.get('an',0)*sab + t.get('voll',0)*s24 + t.get('ab',0)*sab
    elif dp and dp.get('vma_aus', 0) > 0:
        vma_aus = dp.get('vma_aus', 0)
    else:
        vma_aus = inferred.get('vma_aus', 0)

    pendler = PENDLER_BY_YEAR.get(year_int, PENDLER_BY_YEAR[2025])
    reinig_satz = REINIGUNG_PRO_TAG_BY_YEAR.get(year_int, 1.60)
    trink_satz  = TRINKGELD_PRO_NACHT_BY_YEAR.get(year_int, 3.60)

    # VMA = Werbungskosten-Anspruch nach §9 EStG = BMF-Pauschale × Tage.
    # IMMER mit jahr-konformen BMF-Sätzen rechnen — egal was LH stfrei gezahlt hat.
    # Die LH-stfrei-Auszahlung ist Z77, wird separat als Abzug behandelt (§3 Nr. 16 EStG).
    vma_72 = vma_72_tage * bmf_inland['tagestrip_8h']
    vma_73 = vma_73_tage * bmf_inland['an_abreise']
    vma_74 = vma_74_tage * bmf_inland['voll_24h']
    vma_in = vma_72 + vma_73 + vma_74

    # ── FAHRTKOSTEN ───────────────────────────────────────────
    # Multi-Mode: User kann Anreise mischen (Auto + Shuttle + ÖPNV gleichzeitig)
    # Frontend sendet anreise als CSV (z.B. "auto,shuttle,oepnv") oder Single-Wert.
    # Jobticket-Erkennung automatisch aus LSB:
    #   Z18 (15% pauschal) > 0 → AG hat Jobticket pauschal versteuert → User darf kein ÖPNV mehr ansetzen
    #   sonst Z17 > 0 → AG-Zuschuss anteilig (wird später vom Netto abgezogen, ÖPNV-Eingabe legitim)
    fahrzeug  = form.get('fahrzeug', 'verbrenner')
    z18_pauschal = float((lst or {}).get('ag_fahrt_z18_pauschal', 0) or 0) if lst else 0
    jobticket = 'ja_frei' if z18_pauschal > 0 else form.get('jobticket', 'nein')
    fahr = 0.0
    fahr_breakdown = []
    anreise_modes = set(m.strip() for m in str(anreise).split(',') if m.strip())
    if not anreise_modes:
        anreise_modes = {'auto'}

    # Auto/Fahrrad: km × Tage × Pendlerpauschale
    if 'auto' in anreise_modes or 'fahrrad' in anreise_modes:
        f_auto = round(min(km,20)*fahr_tage*pendler['lt_20km'] + max(0,km-20)*fahr_tage*pendler['gt_21km'], 2)
        if f_auto > 0:
            fahr += f_auto
            fahr_breakdown.append(f"Auto/Fahrrad ({km}km × {fahr_tage}T): {f_auto:.2f}€")

    # ÖPNV: Jahreskosten direkt (außer wenn Jobticket frei → 0)
    if 'oepnv' in anreise_modes:
        oepnv_k = float(form.get('oepnv_kosten', 0) or 0)
        f_oepnv = 0 if jobticket == 'ja_frei' else oepnv_k
        if f_oepnv > 0:
            fahr += f_oepnv
            fahr_breakdown.append(f"ÖPNV: {f_oepnv:.2f}€")

    # Shuttle: Jahreskosten direkt (Sammeltaxi/kostenpflichtiger Shuttle)
    if 'shuttle' in anreise_modes or 'shuttle_kosten' in anreise_modes:
        f_shuttle = float(form.get('shuttle_kosten', 0) or 0)
        if f_shuttle > 0:
            fahr += f_shuttle
            fahr_breakdown.append(f"Shuttle: {f_shuttle:.2f}€")

    fahr = round(fahr, 2)
    if fahr_breakdown:
        print(f"[fahrtkosten] {fahr:.2f}€ aus: {', '.join(fahr_breakdown)}")

    # ── Z72-BOOST: pro-Tag Block-Time-aware mit LH-Faustregel-Briefing ──
    # LH Cabin-Crew Faustregel (wenn nicht aus Dienstplan ablesbar):
    #   - Kurzstrecke (Block ≤4h):  85 Min Briefing  (1:25 h vor STD)
    #   - Langstrecke (Block >4h): 110 Min Briefing  (1:50 h vor STD)
    # Sign-Off (Nacharbeitung) ist bei LH einheitlich ~30 Min.
    # Anfahrt: aus km × 1,5 min/km, oder 30 min Default.
    NACHARB_MIN = 30
    user_anfahrt = int(form.get('anfahrt_min', 0) or 0)
    if user_anfahrt > 0:
        anfahrt_min = user_anfahrt
    else:
        anfahrt_min = max(0, int(km * 1.5)) if km > 0 else 30
    if dp:
        candidates = (dp or {}).get('z72_candidates') or []
        qualifying = []
        for cand in candidates:
            block_m = cand.get('block_min', 0)
            # 1) Echte Briefingzeit aus Dienstplan, falls verfügbar
            briefing_min = cand.get('briefing_min')
            # 2) Sonst LH-Faustregel: Kurz/Lang anhand Block-Time
            if not briefing_min:
                briefing_min = 85 if block_m <= 240 else 110
            abw = anfahrt_min + briefing_min + block_m + NACHARB_MIN + anfahrt_min
            if abw >= 480:
                qualifying.append({**cand, 'abwesenheit_min': abw,
                                   'briefing_used': briefing_min})
        if len(qualifying) > vma_72_tage:
            added = len(qualifying) - vma_72_tage
            vma_72_tage = len(qualifying)
            vma_72 = vma_72_tage * bmf_inland['tagestrip_8h']
            vma_in = vma_72 + vma_73 + vma_74
            notes.append(
                f'ℹ Pro-Tag-Berechnung: {added} Inland-Tagestrips erreichen §9-EStG 8h-Schwelle '
                f'(LH-Faustregel Briefing 85/110 min + 30 min Sign-Off + Anfahrt 2×{anfahrt_min}min) → '
                f'+{added*int(bmf_inland["tagestrip_8h"])}€.'
            )

    # ── REINIGUNG & TRINKGELD (jahr-konform) ─────────────────
    reinig = round(arbeitstage * reinig_satz, 2)
    trink  = round(hotel_naechte * trink_satz, 2)

    # ── OPTIONALE BELEGE (User-Upload — Telefon, Gewerkschaft, etc) ──
    opt_keys = ['stb','gew','arb','fort','tel','konz',
                'lapt','fach','reini','bewer',
                'bu','haft','kv','rv','leb','haus','arzt','zahn','medi','pfle','under',
                'kata','spen','part','kind','hand','haed']
    opt_files = {k: files[k] for k in opt_keys if files.get(k)}
    optionale_belege = parse_optionale_belege(opt_files) if opt_files else []

    # ── WERBUNGSKOSTEN-BELEGE zu gesamt addieren ────────────
    # Belege mit wiso='Werbungskosten...' fließen in den WK-Topf (Anlage N).
    # Belege für Sonderausgaben/außergew. Belastungen/Vorsorge gehen separat → nicht hier addieren.
    opt_zu_gesamt = 0.0
    opt_wk_summary = []
    for b in (optionale_belege or []):
        wiso = b.get('wiso', '') or ''
        if not wiso.startswith('Werbungskosten'):
            continue
        betrag = float(b.get('betrag', 0) or 0)
        # Sonderfall Telefon: nur 20% berufl. Anteil (BFH 11.10.2007)
        if b.get('key') == 'tel':
            betrag = round(betrag * 0.20, 2)
        if betrag > 0:
            opt_zu_gesamt += betrag
            opt_wk_summary.append(f"{b.get('name','?')}={betrag:.2f}€")
    opt_zu_gesamt = round(opt_zu_gesamt, 2)
    if opt_zu_gesamt > 0:
        notes.append(f'+ Werbungskosten-Belege ({", ".join(opt_wk_summary)}) +{opt_zu_gesamt:.2f}€')
        print(f"[opt-belege] WK-Beträge: {opt_wk_summary}, total +{opt_zu_gesamt:.2f}€")

    # ── GESAMTBERECHNUNG ─────────────────────────────────────
    gesamt = round(fahr + reinig + trink + vma_in + vma_aus + opt_zu_gesamt, 2)

    # ── NETTO-BERECHNUNG nach §9 EStG + §3 Nr. 16 EStG ──
    # WICHTIG: stfrei-Erstattungen vom AG dürfen NUR den eigenen Topf reduzieren,
    # nicht den Gesamt-Topf. Wenn LH mehr stfrei zahlt als BMF-Pauschale erlaubt
    # (z.B. großzügiges LH-Niveau), ist der Überschuss steuerpflichtig (im Brutto)
    # — User darf nicht zusätzlich noch Werbungskosten geltend machen, aber auch
    # nicht NEGATIVE Werbungskosten.
    vma_total = round(vma_in + vma_aus, 2)
    vma_netto = round(max(0, vma_total - z77), 2)        # Z77 deckelt nur Reisekosten
    fahr_netto = round(max(0, fahr - ag_z17), 2)         # Z17 deckelt nur Fahrtkosten
    netto = round(fahr_netto + reinig + trink + vma_netto + opt_zu_gesamt, 2)
    # Hinweis-Notes wenn AG mehr stfrei gezahlt hat als Pauschale erlaubt
    if z77 > vma_total + 5:
        notes.append(
            f'ℹ Steuerfreie Spesen wurden berücksichtigt: {z77:.2f} €. '
            f'Berechnete VMA-Pauschalen: {vma_total:.2f} €. '
            f'Da die steuerfreien Spesen die berechneten VMA-Pauschalen übersteigen, '
            f'ergibt sich für den Reisekosten-Topf kein zusätzlicher Betrag.'
        )
    if ag_z17 > fahr + 5:
        notes.append(
            f'ℹ Arbeitgeber-Fahrkostenzuschuss (Zeile 17): {ag_z17:.2f} €. '
            f'Berechnete Fahrtkosten: {fahr:.2f} €. '
            f'Da der Zuschuss die Fahrtkosten übersteigt, ergibt sich für den '
            f'Fahrtkosten-Topf kein zusätzlicher Betrag.'
        )

    # ── MATHEMATISCHE PLAUSI-CHECKS ──────────────────────────
    # Wenn Inkonsistenzen, User über Note informieren — keine stille Fehler.
    plausi_warns = []
    # Z72+Z73+Z74+Z76 ≈ Z77 (alle steuerfreien Werte sollten Z77 ergeben)
    # Bei sauber geparstem SE (keine unklare_zeilen) sollten sie nahezu identisch sein.
    vma_summe = vma_72 + vma_73 + vma_74 + vma_aus
    se_unklar_count = len((se_data or {}).get('unklare_zeilen', []) or [])
    # Tolerance: 1% wenn SE clean, 5% wenn unklare Zeilen, min 30€
    # Tolerance erhöht bei Auslandstouren (Z76 > 0): bei vielen Auslandsdestinationen
    # können BMF-Pauschalen und LH-stfrei systematisch differieren (nicht-1:1-Mapping).
    # Defaults: 1% wenn SE clean ohne Ausland, 5% wenn unklare Zeilen, 10% bei Auslandstouren.
    has_ausland = vma_aus > 0
    if has_ausland and se_unklar_count > 5:
        tol_pct = 0.10
    elif has_ausland:
        tol_pct = 0.05  # statt 0.01 — Ausland erlaubt mehr Drift
    elif se_unklar_count > 5:
        tol_pct = 0.05
    else:
        tol_pct = 0.01
    tolerance = max(30, z77 * tol_pct)
    if z77 > 0 and abs(vma_summe - z77) > tolerance:
        plausi_warns.append(f'VMA-Summe ({vma_summe:.2f}€) weicht von Z77 ({z77:.2f}€) um {abs(vma_summe-z77):.2f}€ ab — bitte prüfen.')
    # Hotel ≤ Arbeitstage (logisch: jede Hotelnacht ist auch ein Arbeitstag)
    if hotel_naechte > arbeitstage:
        plausi_warns.append(f'Hotelnächte ({hotel_naechte}) > Arbeitstage ({arbeitstage}) — unmöglich, bitte prüfen.')
    # Fahrtage ≤ Arbeitstage
    if fahr_tage > arbeitstage:
        plausi_warns.append(f'Fahrtage ({fahr_tage}) > Arbeitstage ({arbeitstage}) — unmöglich, bitte prüfen.')
    # Arbeitstage ≤ 365
    if arbeitstage > 365:
        plausi_warns.append(f'Arbeitstage ({arbeitstage}) > 365 — bitte prüfen.')
    if plausi_warns:
        notes.extend(['⚠ ' + w for w in plausi_warns])
        print(f"PLAUSI-CHECKS fehlgeschlagen: {plausi_warns}")
    else:
        print(f"PLAUSI-CHECKS ok: VMA-Summe={vma_summe:.2f}€ Z77={z77:.2f}€ Hotel/Arbeit/Fahr/365 alle plausibel")

    # ── EINSATZPLAN-CROSS-CHECK (wenn vorhanden) ─────────────
    if einsatz_data and einsatz_data.get('monate_geparst', 0) > 0:
        ein_monate = einsatz_data['monate_geparst']
        ein_spesen = einsatz_data['spesen_total']
        ein_umlaeufe_count = len(einsatz_data.get('umlaeufe', []))
        cross_notes = []

        # Cross-Check 1: Spesen-Total nur vergleichen, wenn beide Quellen die gleichen Monate abdecken
        # (sonst vergleichen wir Äpfel mit Birnen — z.B. Einsatzplan 12 Mon vs SE 8 Mon Teilzeit)
        if se_data and se_data.get('summe_gesamt', 0) > 0:
            se_monate = {int(a.get('monat', 0) or 0) for a in se_data.get('abrechnungen', []) if a.get('monat')}
            # Nur vergleichen wenn Einsatzplan-Monate ⊆ SE-Monate (oder umgekehrt vollständig)
            gemeinsame_monate = einsatz_abgedeckte_monate & se_monate if se_monate else set()
            if gemeinsame_monate and gemeinsame_monate == einsatz_abgedeckte_monate and ein_monate >= 6:
                se_spesen = float(se_data.get('summe_gesamt', 0))
                spesen_diff = abs(ein_spesen - se_spesen)
                spesen_tolerance = max(50, se_spesen * 0.03)
                # Nur wenn ALLE 12 Monate von beiden abgedeckt sind, sonst Vergleich nicht sinnvoll
                if spesen_diff > spesen_tolerance and len(einsatz_abgedeckte_monate) == 12 and len(se_monate) == 12:
                    cross_notes.append(
                        f'Spesen-Differenz: Einsatzplan zeigt {ein_spesen:.2f}€ — '
                        f'Streckeneinsatz {se_spesen:.2f}€ (Δ {spesen_diff:.2f}€). '
                        f'Möglicherweise wurde ein Wert nicht richtig ausgelesen — bitte prüfen.'
                    )

        # Cross-Check 2: ist bereits oben in der SE-Warning-Logik abgedeckt
        # (Vergleich SE-Flugmonate vs Einsatzplan-Flugmonate)
        # → hier nichts mehr tun, sonst Doppel-Warnung

        # Cross-Check 3: Vollständigkeit Einsatzplan-Monate
        if 1 <= ein_monate < 12:
            cross_notes.append(
                f'Einsatzplan: nur {ein_monate}/12 Monate hochgeladen — '
                f'der Cross-Check läuft nur über die hochgeladenen Monate.'
            )

        if cross_notes:
            notes.extend(['↻ Einsatzplan-Abgleich: ' + n for n in cross_notes])
            print(f"EINSATZPLAN-CROSS-CHECK Hinweise: {cross_notes}")
        else:
            ok_msg = (f'Einsatzplan-Abgleich: {ein_monate}/12 Monate, '
                      f'{ein_umlaeufe_count} Umläufe, Spesen {ein_spesen:.2f}€ — alle Werte konsistent.')
            notes.append('✓ ' + ok_msg)
            print(f"EINSATZPLAN-CROSS-CHECK ok: {ok_msg}")

    # ── QUALITY GATE: bei massiven Plausi-Fehlern direkt Recovery-Hinweis aufnehmen ──
    quality_questionable = bool(plausi_warns) and any('unmöglich' in w.lower() for w in plausi_warns)
    if quality_questionable:
        notes.append('🔁 Qualitäts-Hinweis: Werte sehen unplausibel aus. Falls offensichtlich falsch — du bekommst nach Auswertung einen kostenlosen Wiederholungs-Code.')

    # ── ANOMALIE-DETECTION: Werte außerhalb realistischer Bandbreite ──
    # Ausfallzeit (Mutterschutz/Krank/Teilzeit) berücksichtigen — Bandbreiten skalieren proportional
    ausfall_monate = int(form.get('ausfallzeit_monate', 0) or 0)
    arbeits_quote = max(0.1, (12 - ausfall_monate) / 12)  # min 10% damit nicht /0
    anomalies = []
    if arbeitstage > round(250 * arbeits_quote):
        anomalies.append(f'Arbeitstage {arbeitstage} sehr hoch — bitte prüfen')
    if fahr_tage > arbeitstage and arbeitstage > 0:
        anomalies.append(f'Fahrtage {fahr_tage} > Arbeitstage {arbeitstage} — unmöglich')
    if hotel_naechte > round(120 * arbeits_quote):
        anomalies.append(f'Hotelnächte {hotel_naechte} sehr hoch — bitte prüfen')
    if vma_aus > round(15000 * arbeits_quote):
        anomalies.append(f'VMA Ausland {vma_aus:.0f}€ sehr hoch — bitte prüfen')
    if ausfall_monate > 0:
        notes.append(f'ℹ Ausfallzeit angegeben: {ausfall_monate} Monat(e) — Plausi-Bandbreiten entsprechend angepasst.')
    if anomalies:
        for a in anomalies:
            notes.append(f'⚠ Anomalie: {a}')
        print(f"ANOMALIE-DETECTION (Quote {arbeits_quote:.2f}): {anomalies}")

    # ── AUDIT-TRAIL ──────────────────────────────────────────
    se_unklar = len((se_data or {}).get('unklare_zeilen', []))
    se_clean = se_data is not None and se_unklar == 0
    flug_clean = (dp or {}).get('_flug_clean', False)
    flug_unklar = len(((dp or {}).get('_flug_parser') or {}).get('unklare_tage', []))
    opus_used = (dp or {}).get('_opus_used', False)
    verif_src = (dp or {}).get('_verification_source', 'unbekannt')

    # ── CONFIDENCE SCORING pro Wert ──
    # 100% = deterministisch + Math-OK; 85-95% = AI-Konsensus; 70-84% = Hybrid mit Drift; <70% = Unsicher
    def _conf(deterministic, agreement=True, math_ok=True):
        if deterministic and math_ok: return 100
        if deterministic and not math_ok: return 85
        if agreement and math_ok: return 92
        if agreement and not math_ok: return 78
        return 65

    math_ok = not plausi_warns
    confidence = {
        'z77':         _conf(True, math_ok=math_ok),
        'z76':         _conf(se_clean, agreement=opus_used, math_ok=math_ok),
        'z72':         _conf(se_clean, agreement=opus_used, math_ok=math_ok),
        'z73':         _conf(se_clean, agreement=opus_used, math_ok=math_ok),
        'fahrtage':    _conf(flug_clean, agreement=opus_used, math_ok=math_ok),
        'arbeitstage': _conf(flug_clean, agreement=opus_used, math_ok=math_ok),
        'hotel':       _conf(flug_clean, agreement=opus_used, math_ok=math_ok),
        'lsb':         _conf(True, math_ok=True),
    }
    audit_source = {
        'z77':         'deterministisch — Summe-Zeile aus SE-Abrechnungen',
        'z76':         f'deterministisch — Σ stfrei-Werte aus {len(se_data.get("abrechnungen", [])) if se_data else 0} SE-Monaten' if se_clean else 'Hybrid (Parser + Claude)',
        'z72':         'deterministisch — SE-Line-Parser' if se_clean else 'Hybrid (Parser + Claude)',
        'z73':         'deterministisch — SE-Line-Parser' if se_clean else 'Hybrid (Parser + Claude)',
        'fahrtage':    f'Parser + Sonnet 4.6 + {"Opus 4.7 verifiziert" if opus_used else "Übereinstimmung ohne Konflikt"}',
        'arbeitstage': f'Parser + Sonnet 4.6 + {"Opus 4.7 verifiziert" if opus_used else "Übereinstimmung ohne Konflikt"}',
        'hotel':       f'Parser + Sonnet 4.6 + {"Opus 4.7 verifiziert (EASA-FTL)" if opus_used else "Übereinstimmung ohne Konflikt"}',
    }
    verification_info = {
        'parser_clean': se_clean,
        'se_unklar':    se_unklar,
        'flug_clean':   flug_clean,
        'flug_unklar':  flug_unklar,
        'opus_used':    opus_used,
        'plausi_ok':    not plausi_warns,
        'verif_source': verif_src,
    }

    # ── UPLOADED DOCS SUMMARY ────────────────────────────────
    uploaded_summary = []
    not_uploaded = []
    if files.get('lsb'):  uploaded_summary.append(f"LSB ({len(files['lsb'])} Datei(en))")
    else: not_uploaded.append("Lohnsteuerbescheinigung")
    if files.get('dp'):   uploaded_summary.append(f"Flugstunden ({len(files['dp'])} Datei(en))")
    else: not_uploaded.append("Flugstunden-Übersichten")
    if files.get('se'):   uploaded_summary.append(f"Streckeneinsatz ({len(files['se'])} Datei(en))")
    else: not_uploaded.append("Streckeneinsatz-Abrechnungen")

    # optionale_belege bereits vor Pauschalen-Logik geparst

    # ── OPUS-FINAL-AUDIT: Senior-Werbungskosten-Cross-Check aller Werte ──
    audit_input = {
        'brutto': brutto, 'lohnsteuer': lohnsteuer, 'ag_fahrt_z17': ag_z17,
        'verpflegungszuschuss_z20': verpfl_z20,
        'z77': z77, 'vma_aus': vma_aus,
        'vma_72_tage': vma_72_tage, 'vma_72': vma_72,
        'vma_73_tage': vma_73_tage, 'vma_73': vma_73,
        'vma_74_tage': vma_74_tage, 'vma_74': vma_74,
        'arbeitstage': arbeitstage, 'fahr_tage': fahr_tage,
        'hotel_naechte': hotel_naechte, 'fahr': fahr,
        'reinig': reinig, 'trink': trink,
        'gesamt': gesamt, 'netto': netto,
    }
    try:
        opus_issues = _opus_final_audit(audit_input, available_texts, int(form.get('year', 2025)))
    except Exception as _ae:
        print(f"[Opus-Audit] crash: {_ae}")
        opus_issues = []
    gc.collect()  # Opus-Response + Audit-Texte freigeben

    # Critical Issues mit konkreter Korrektur → automatisch übernehmen + Note
    # Minor Issues / unsichere Korrekturen → nur als Warnung anzeigen
    auto_corrections = []
    for issue in opus_issues:
        feld = issue.get('feld', '')
        aktuell = issue.get('aktuell')
        korrekt = issue.get('korrekt')
        grund = issue.get('grund', '')
        sev = issue.get('severity', 'minor')

        if korrekt is None:
            # Nur Warnung
            notes.append(f'⚠ Senior-Audit ({feld}): {grund}')
            continue

        # Auto-Korrektur konservativ: nur kleine Diffs (<15%) und ≥1€/1 Tag.
        # Geld-Diff niemals stillschweigend > 100€ anwenden — sonst nur Warnung.
        try:
            cur_v = float(aktuell or 0)
            new_v = float(korrekt)
            diff_pct = abs(new_v - cur_v) / max(abs(cur_v), 1.0)
            money_fields = {'z77', 'vma_aus', 'vma_72', 'vma_73', 'vma_74', 'brutto', 'lohnsteuer', 'ag_fahrt_z17', 'ag_z17'}
            big_money_diff = feld in money_fields and abs(new_v - cur_v) > 100
            if sev == 'critical' and diff_pct < 0.15 and abs(new_v - cur_v) >= 1.0 and not big_money_diff:
                # Korrektur anwenden — alle Felder die Opus vorschlagen kann
                applied = False
                if feld == 'z77' and abs(new_v - z77) > 1:
                    z77 = new_v; applied = True
                elif feld == 'vma_aus' and abs(new_v - vma_aus) > 1:
                    vma_aus = new_v; applied = True
                elif feld == 'vma_72' and abs(new_v - vma_72) > 1:
                    vma_72 = new_v; applied = True
                elif feld == 'vma_73' and abs(new_v - vma_73) > 1:
                    vma_73 = new_v; applied = True
                elif feld == 'vma_74' and abs(new_v - vma_74) > 1:
                    vma_74 = new_v; applied = True
                elif feld == 'arbeitstage' and abs(new_v - arbeitstage) >= 1:
                    arbeitstage = int(new_v); applied = True
                elif feld == 'fahr_tage' and abs(new_v - fahr_tage) >= 1:
                    fahr_tage = int(new_v); applied = True
                elif feld == 'hotel_naechte' and abs(new_v - hotel_naechte) >= 1:
                    hotel_naechte = int(new_v); applied = True
                elif feld == 'brutto' and abs(new_v - brutto) > 1:
                    brutto = new_v; applied = True
                elif feld in ('ag_fahrt_z17', 'ag_z17') and abs(new_v - ag_z17) > 1:
                    ag_z17 = new_v; applied = True
                if applied:
                    auto_corrections.append(f'{feld}: {cur_v:.2f} → {new_v:.2f}')
                    notes.append(f'↻ Senior-Korrektur ({feld}): {cur_v:.2f} → {new_v:.2f} — {grund}')
                else:
                    notes.append(f'⚠ Senior-Audit ({feld}): {aktuell} → {korrekt}? Grund: {grund}')
            else:
                # Keine Auto-Anwendung — nur Hinweis (zu großer Geld-Diff oder zu unsicher)
                notes.append(f'⚠ Senior-Audit ({feld}): aktuell {aktuell}, möglicherweise korrekt {korrekt} — {grund}')
        except Exception:
            notes.append(f'⚠ Senior-Audit ({feld}): {grund}')

    # Bei Auto-Korrekturen ALLE abgeleiteten Werte neu berechnen, sonst Math-Inkonsistenz
    if auto_corrections:
        reinig = round(arbeitstage * reinig_satz, 2)
        trink  = round(hotel_naechte * trink_satz, 2)
        # Fahrtkosten neu (Auto-Komponente: km/fahr_tage könnten sich geändert haben)
        if 'auto' in anreise_modes or 'fahrrad' in anreise_modes:
            f_auto_new = round(min(km, 20) * fahr_tage * pendler['lt_20km'] +
                               max(0, km - 20) * fahr_tage * pendler['gt_21km'], 2)
            # Anteilig: alte Auto-Komponente abziehen, neue rauf
            f_auto_old = next((float(p.split(': ')[1].rstrip('€'))
                               for p in fahr_breakdown if p.startswith('Auto')), 0)
            fahr = round(fahr - f_auto_old + f_auto_new, 2)
        # VMA-Tage rückrechnen falls Opus EUR-Werte korrigiert hat (sonst Inkonsistenz EUR/Tage)
        if any(c.startswith('vma_72:') for c in auto_corrections):
            vma_72_tage = round(vma_72 / max(bmf_inland['tagestrip_8h'], 0.01))
        if any(c.startswith('vma_73:') for c in auto_corrections):
            vma_73_tage = round(vma_73 / max(bmf_inland['an_abreise'], 0.01))
        if any(c.startswith('vma_74:') for c in auto_corrections):
            vma_74_tage = round(vma_74 / max(bmf_inland['voll_24h'], 0.01))
        # vma_in neu (Inland-Anteile könnten korrigiert sein)
        vma_in = round(vma_72 + vma_73 + vma_74, 2)
        # Wenn z77 korrigiert wurde: spesen_gesamt/spesen_steuer angleichen damit PDF-Math stimmt
        # (spesen_gesamt = z77 + spesen_steuer; bei z77-Korrektur halten wir spesen_steuer fix)
        if any(c.startswith('z77:') for c in auto_corrections):
            spesen_gesamt = round(z77 + spesen_steuer, 2)
        gesamt = round(fahr + reinig + trink + vma_in + vma_aus + opt_zu_gesamt, 2)
        # Netto wieder mit getrennter Topf-Logik (Z77 nur gegen VMA, Z17 nur gegen Fahrt)
        vma_total = round(vma_in + vma_aus, 2)
        vma_netto = round(max(0, vma_total - z77), 2)
        fahr_netto = round(max(0, fahr - ag_z17), 2)
        netto = round(fahr_netto + reinig + trink + vma_netto + opt_zu_gesamt, 2)
        print(f"[Opus-Audit] Auto-Korrekturen: {auto_corrections}; "
              f"recalc: reinig={reinig:.2f} trink={trink:.2f} fahr={fahr:.2f} "
              f"vma_in={vma_in:.2f} gesamt={gesamt:.2f} netto={netto:.2f} "
              f"(vma_netto={vma_netto:.2f}, fahr_netto={fahr_netto:.2f})")

    # Notes-Deduplikation: identische Strings entfernen, Reihenfolge erhalten
    if notes:
        _seen = set()
        notes = [n for n in notes if not (n in _seen or _seen.add(n))]

    return {
        'name':             form.get('name', 'Flugbegleiter'),
        'year':             form.get('year', 2025),
        '_isDemo': False,
        'uploaded_summary': ', '.join(uploaded_summary),
        'not_uploaded':     ', '.join(not_uploaded) if not_uploaded else 'Alle Pflichtdokumente vorhanden',
        'notes':            notes,
        'datum':            datetime.now().strftime('%d.%m.%Y'),
        # Reisedaten
        'km':               km,
        'arbeitstage':      arbeitstage,
        'fahr_tage':        fahr_tage,
        'hotel_naechte':    hotel_naechte,
        # VMA
        'vma_72_tage':      vma_72_tage,
        'vma_73_tage':      vma_73_tage,
        'vma_74_tage':      vma_74_tage,
        'vma_72':           vma_72,
        'vma_73':           vma_73,
        'vma_74':           vma_74,
        'vma_in':           vma_in,
        'vma_aus':          vma_aus,
        # Kostenposten
        'fahr':             fahr,
        'reinig':           reinig,
        'trink':            trink,
        'gesamt':           gesamt,
        # Abzüge
        'ag_z17':           ag_z17,
        'spesen_gesamt':    spesen_gesamt,
        'spesen_steuer':    spesen_steuer,
        'z77':              z77,
        'netto':            netto,
        # Abrechnungen
        'abrechnungen':     abrechnungen,
        # LSB — Grunddaten
        'brutto':           brutto,
        'lohnsteuer':       lohnsteuer,
        'soli':             soli,
        'kirchensteuer':    kirchensteuer,
        'steuerklasse':     steuerklasse,
        'kinderfreibetraege': kinderfb,
        'identnr':          identnr,
        'geburtsdatum':     geburtsdatum,
        'personalnummer':   personalnummer,
        'arbeitgeber':      arbeitgeber,
        # LSB — Sozialversicherung (Sonderausgaben)
        'rv_an':            rv_an,
        'rv_ag':            rv_ag,
        'rv_gesamt':        rv_gesamt,
        'kv_an':            kv_an,
        'pv_an':            pv_an,
        'av_an':            av_an,
        'vorsorge_gesamt_an': vorsorge_an,
        # Steuerfreie AG-Leistungen
        'verpfl_z20':       verpfl_z20,
        # Optionale Belege
        'optionale_belege': optionale_belege,
        # Audit-Trail + Confidence
        '_audit_source':   audit_source,
        '_verification':   verification_info,
        '_confidence':     confidence,
        '_anomalies':      anomalies,
    }


def _fallback_streck():
    """Kein Fallback mehr — echte PDFs werden benötigt."""
    raise ValueError(
        "Streckeneinsatz-Abrechnungen konnten nicht ausgelesen werden. "
        "Bitte stelle sicher dass alle 12 Monate hochgeladen sind."
    )


# ══════════════════════════════════════════════════════════════════
#  PDF GENERIERUNG
#  Helles Design — Steuerauswertung
#  Korrekter Aufbau nach Standard-Methode
# ══════════════════════════════════════════════════════════════════













def erstelle_pdf(d):
    # ── PALETTE: dezentes Dark Navy — minimalistisch & elegant ──
    BG       = HexColor("#060a16")   # dunkles Navy als Page-BG
    BG_DARK  = HexColor("#0a1224")   # leicht dunkler für Header
    BG_DEEP  = HexColor("#040810")   # Footer
    BG_CARD  = HexColor("#0f1830")   # subtle "card" tint
    TEXT     = HexColor("#f1f5f9")   # primary white
    TEXT2    = HexColor("#94a3b8")   # secondary
    TEXT3    = HexColor("#4a5a72")   # muted
    TEXT_D   = HexColor("#1e3a8a")   # (für legacy white-card calls — unused)
    TEXT_D2  = HexColor("#3b5cae")
    TEXT_D3  = HexColor("#64748b")
    LINE     = HexColor("#1e3050")
    LINE2    = HexColor("#2a3f5e")
    LINE_W   = HexColor("#1e3050")
    WHITE    = HexColor("#ffffff")
    BLUE2    = HexColor("#60a5fa")
    BLUE3    = HexColor("#93c5fd")
    BLUE_HL  = HexColor("#60a5fa")
    NAVY     = HexColor("#0a1224")
    OFF      = HexColor("#e2e8f0")
    GOLD     = HexColor("#fbbf24")
    G1=HexColor("#f97316"); G2=HexColor("#ec4899")
    G3=HexColor("#8b5cf6"); G4=HexColor("#2563eb")

    base = getSampleStyleSheet()
    def ps(n, **kw): return ParagraphStyle(n, parent=base["Normal"], **kw)

    def eur(n):
        v = float(n or 0)
        s = f"{abs(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")
        return ("− " if v < 0 else "") + s + " €"

    def hr(before=0, after=16, width="100%", thick=0.4, color=None):
        return HRFlowable(width=width, thickness=thick,
            color=color or LINE, spaceBefore=before, spaceAfter=after)

    def section(title):
        """Elegant section break — like chapter opener"""
        return [
            Spacer(1, 0.3*cm),
            Paragraph(title,
                ps(f"sec{id(title)}", fontSize=8, textColor=TEXT3,
                   fontName="Helvetica-Bold", leading=11,
                   spaceAfter=14, letterSpacing=2.0)),
            HRFlowable(width="100%", thickness=0.4, color=LINE,
                spaceAfter=18),
        ]

    def row_item(label, value, label_color=None, value_color=None, value_size=9):
        """Clean label + value, separated by dots, no table border"""
        return Table([[
            Paragraph(label,
                ps(f"rl{id(label)}", fontSize=9, textColor=label_color or TEXT2,
                   fontName="Helvetica", leading=13)),
            Paragraph(value,
                ps(f"rv{id(value)}", fontSize=value_size,
                   textColor=value_color or TEXT,
                   fontName="Helvetica", leading=13, alignment=TA_RIGHT)),
        ]], colWidths=[11.5*cm, 5.3*cm])._setStyleHelper(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),5),
            ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEBELOW",(0,0),(-1,0),0.3,LINE),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))

    # Helper that actually works
    def kv(label, value, lc=None, vc=None, vs=9, bold=False):
        t = Table([[
            Paragraph(label,
                ps(f"kl{id(label)}", fontSize=9, textColor=lc or TEXT2,
                   fontName="Helvetica", leading=13)),
            Paragraph(value,
                ps(f"kv{id(value)}", fontSize=vs,
                   textColor=vc or TEXT,
                   fontName="Helvetica-Bold" if bold else "Helvetica",
                   leading=13, alignment=TA_RIGHT)),
        ]], colWidths=[11.5*cm, 5.3*cm])
        t.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),6), ("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEBELOW",(0,0),(-1,0),0.3,LINE),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))
        return t

    def kv_total(label, value):
        t = Table([[
            Paragraph(label,
                ps(f"kt{id(label)}", fontSize=10, textColor=TEXT,
                   fontName="Helvetica-Bold", leading=14)),
            Paragraph(value,
                ps(f"kvt{id(value)}", fontSize=14,
                   textColor=TEXT, fontName="Helvetica-Bold",
                   leading=18, alignment=TA_RIGHT)),
        ]], colWidths=[11.5*cm, 5.3*cm])
        t.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),10), ("BOTTOMPADDING",(0,0),(-1,-1),10),
            ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEABOVE",(0,0),(-1,0),0.8,LINE2),
            ("LINEBELOW",(0,0),(-1,0),0.8,LINE2),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))
        return t

    # ── WHITE BROCHURE CARD HELPERS (für Berechnung/Bestätigung) ──
    def white_card(inner_flowables, pad=18, width_cm=16.8):
        """Wrappt Flowables in eine weiße Brochure-Card auf blauem BG."""
        t = Table([[inner_flowables]], colWidths=[width_cm*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,-1),BG_CARD),
            ('LEFTPADDING',(0,0),(-1,-1),pad),
            ('RIGHTPADDING',(0,0),(-1,-1),pad),
            ('TOPPADDING',(0,0),(-1,-1),pad),
            ('BOTTOMPADDING',(0,0),(-1,-1),pad),
            ('VALIGN',(0,0),(-1,-1),'TOP'),
        ]))
        try:
            t.cornerRadii = [10, 10, 10, 10]
        except Exception:
            pass
        return t

    def kv_dark(label, value, bold=False, big=False):
        """KV row für white card — dunkler Text auf weiß."""
        vsize = 14 if big else 10
        t = Table([[
            Paragraph(label,
                ps(f"kld{id(label)}", fontSize=10, textColor=TEXT_D2,
                   fontName="Helvetica", leading=14)),
            Paragraph(value,
                ps(f"kvd{id(value)}", fontSize=vsize,
                   textColor=TEXT_D,
                   fontName="Helvetica-Bold" if (bold or big) else "Helvetica",
                   leading=vsize+4, alignment=TA_RIGHT)),
        ]], colWidths=[10.6*cm, 4.6*cm])
        t.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),7), ("BOTTOMPADDING",(0,0),(-1,-1),7),
            ("LEFTPADDING",(0,0),(-1,-1),0), ("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEBELOW",(0,0),(-1,0),0.3,LINE_W),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))
        return t

    # ── PAGE HEADER / FOOTER ─────────────────────────────────
    def on_page(canv, doc):
        canv.saveState()
        W, H = A4
        # Hauptbackground — Royal Blue dominant
        canv.setFillColor(BG); canv.rect(0,0,W,H,fill=1,stroke=0)

        # Header-Bar — dunklere Navy strip mit Logo
        canv.setFillColor(BG_DARK)
        canv.rect(0, H-1.5*cm, W, 1.5*cm, fill=1,stroke=0)

        # Mini AeroTAX-Logo (klein, dezent oben links)
        lx = 1.5*cm; ly = H-1.25*cm; lh = 0.7*cm; lw = lh
        scale = lh/210.0
        def Lpt(x,y): return (lx + x*scale, ly + lh - y*scale)
        canv.setFillColor(WHITE)
        # Linker A-Schenkel
        p = canv.beginPath()
        for i,(x,y) in enumerate([(18,192),(76,22),(94,22),(100,42),(46,192)]):
            if i==0: p.moveTo(*Lpt(x,y))
            else: p.lineTo(*Lpt(x,y))
        p.close(); canv.drawPath(p, fill=1, stroke=0)
        # Rechter A-Schenkel
        p = canv.beginPath()
        for i,(x,y) in enumerate([(182,192),(124,22),(106,22),(100,42),(154,192)]):
            if i==0: p.moveTo(*Lpt(x,y))
            else: p.lineTo(*Lpt(x,y))
        p.close(); canv.drawPath(p, fill=1, stroke=0)
        # Cross-Bar
        canv.rect(*Lpt(52,144), 96*scale, 16*scale, fill=1, stroke=0)
        # Flugzeug-Symbol über dem A — Cockpit + Tragflächen
        canv.setFillColor(WHITE)
        # Cockpit-Mast (rect) — kürzer damit Antenne im Header bleibt
        cx,cy = Lpt(96.5, 30)
        canv.rect(cx, cy, 7*scale, 30*scale, fill=1, stroke=0)
        # Cockpit-Spitze (ellipse oben) — Y im positiven Bereich, kein Overflow
        canv.ellipse(lx+95*scale, ly+lh-2*scale,
                     lx+105*scale, ly+lh-12*scale, fill=1, stroke=0)
        # Linke Tragfläche
        p = canv.beginPath()
        for i,(x,y) in enumerate([(100,14),(100,25),(56,36),(60,25)]):
            if i==0: p.moveTo(*Lpt(x,y))
            else: p.lineTo(*Lpt(x,y))
        p.close(); canv.drawPath(p, fill=1, stroke=0)
        # Rechte Tragfläche
        p = canv.beginPath()
        for i,(x,y) in enumerate([(100,14),(100,25),(144,36),(140,25)]):
            if i==0: p.moveTo(*Lpt(x,y))
            else: p.lineTo(*Lpt(x,y))
        p.close(); canv.drawPath(p, fill=1, stroke=0)
        # Triebwerke (gold) — kleine Akzente
        canv.setFillColor(GOLD)
        ex,ey = Lpt(67, 30.5)
        canv.rect(ex, ey, 13*scale, 4.5*scale, fill=1, stroke=0)
        ex,ey = Lpt(120, 30.5)
        canv.rect(ex, ey, 13*scale, 4.5*scale, fill=1, stroke=0)

        # AeroTAX wordmark rechts vom Logo — komplett weiß, dünn (Helvetica regular)
        canv.setFillColor(WHITE); canv.setFont("Helvetica", 14)
        text_x = lx + lw + 0.25*cm
        canv.drawString(text_x, H-1.0*cm, "AeroTAX")
        aw = canv.stringWidth("AeroTAX","Helvetica",14)
        tw = 0  # combined into aw
        # Trenner + Name
        canv.setFillColor(TEXT3); canv.setFont("Helvetica",9)
        canv.drawString(text_x+aw+tw+0.22*cm, H-1.02*cm, "·")
        canv.setFillColor(TEXT2); canv.setFont("Helvetica",9)
        # Lange Namen truncen damit kein Overflow
        _name_full = d.get('name','') or ''
        _name_short = _name_full if len(_name_full) <= 24 else (_name_full[:23] + '…')
        canv.drawString(text_x+aw+tw+0.55*cm, H-1.0*cm,
            f"{_name_short}  ·  Steuerjahr {d.get('year',2025)}")
        # Page-Number rechts (im Brochure-Stil: "PAGE — 03")
        canv.setFillColor(TEXT3); canv.setFont("Helvetica",8)
        canv.drawRightString(W-1.5*cm, H-1.0*cm,
            f"PAGE — {str(doc.page).zfill(2)}")

        # Footer — schmaler Strip mit URL
        canv.setFillColor(BG_DEEP)
        canv.rect(0, 0, W, 0.65*cm, fill=1,stroke=0)
        canv.setFillColor(TEXT2); canv.setFont("Helvetica",7.5)
        canv.drawString(1.5*cm, 0.42*cm,"aerosteuer.de")
        canv.setFillColor(TEXT3); canv.setFont("Helvetica",7.5)
        canv.drawRightString(W-1.5*cm, 0.42*cm, d.get('datum',''))
        canv.restoreState()

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=1.6*cm, rightMargin=1.6*cm,
        topMargin=2.0*cm, bottomMargin=1.0*cm)

    opt = d.get('optionale_belege',[])
    is_demo = d.get('_isDemo',False)
    belege = [b for b in opt
        if b.get('betrag',0) > 0 or (b.get('file_bytes_list') and not is_demo)]
    has_fotos = not is_demo and any(b.get('file_bytes_list') for b in belege)

    S = []

    # ════════════════════════════════════════════════
    # SEITE 1 — DECKBLATT (dünn, elegant — Montserrat-Vibe)
    # ════════════════════════════════════════════════
    S.append(Spacer(1, 5.5*cm))

    # Eyebrow
    S.append(Paragraph("WERBUNGSKOSTEN-AUSWERTUNG",
        ps("eye", fontSize=8.5, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=12, alignment=TA_CENTER, spaceAfter=24, letterSpacing=2.5)))

    # v9.9: Title generisch, Name nur im Subtitle (User-Direktive: keine Person im Titel).
    _name = d.get('name', '') or ''
    _year = d.get('year', '') or ''
    S.append(Paragraph("Werbungskosten-Auswertung",
        ps("h1", fontSize=20, textColor=TEXT, fontName="Helvetica",
           leading=26, alignment=TA_CENTER, spaceAfter=6, letterSpacing=0)))
    _subtitle = f"{_name} · Steuerjahr {_year}" if _name else f"Steuerjahr {_year}"
    S.append(Paragraph(_subtitle,
        ps("h1y", fontSize=11, textColor=TEXT2, fontName="Helvetica",
           leading=15, alignment=TA_CENTER, spaceAfter=40, letterSpacing=0.6)))

    # Subtle Trenner
    S.append(HRFlowable(width="10%", thickness=0.5, color=LINE2,
        hAlign='CENTER', spaceAfter=24))

    # Lufthansa als kleine Sub-Info (Name ist ja im Title)
    S.append(Paragraph("Deutsche Lufthansa AG",
        ps("cag", fontSize=9.5, textColor=TEXT3, fontName="Helvetica",
           leading=14, alignment=TA_CENTER, spaceAfter=42, letterSpacing=1.2)))

    S.append(HRFlowable(width="10%", thickness=0.5, color=LINE2,
        hAlign='CENTER', spaceAfter=28))

    # Brochure-Style: kompakte "Inhalt"-Liste am unteren Rand des Covers
    S.append(Spacer(1, 0.4*cm))
    S.append(HRFlowable(width="60%", thickness=0.4, color=LINE,
        hAlign='CENTER', spaceAfter=18))
    S.append(Paragraph("In dieser Auswertung",
        ps("toch", fontSize=9, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=13, alignment=TA_CENTER, spaceAfter=14)))

    pg = 2
    toc = [
        ("01", "Reisekosten & weitere absetzbare Kosten", str(pg)),
    ]; pg+=1
    toc += [
        ("02", "Belege & Anlagen", str(pg)),
    ]; pg+=1
    toc += [
        ("03", "Berechnung im Detail", str(pg)),
    ]; pg+=1
    toc += [
        ("04", "Bestätigung & Unterschrift", str(pg)),
    ]

    for i, (num, title, page) in enumerate(toc):
        row = Table([[
            Paragraph(num,
                ps(f"tn{i}", fontSize=11, textColor=BLUE3,
                   fontName="Helvetica-Bold", leading=15, alignment=TA_CENTER)),
            Paragraph(title,
                ps(f"tt{i}", fontSize=10, textColor=TEXT,
                   fontName="Helvetica", leading=14)),
            Paragraph(f"PAGE — {page.zfill(2)}",
                ps(f"tp{i}", fontSize=8, textColor=TEXT3,
                   fontName="Helvetica", leading=12, alignment=TA_RIGHT)),
        ]], colWidths=[0.9*cm, 10.8*cm, 1.5*cm])
        row.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7),
            ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEBELOW",(0,0),(-1,0),0.3,LINE),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))
        wrap = Table([[row]], colWidths=[13.2*cm])
        wrap.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),
            ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
            ("ALIGN",(0,0),(-1,-1),"CENTER"),
        ]))
        S.append(wrap)

    S.append(Spacer(1, 0.8*cm))
    S.append(HRFlowable(width="30%", thickness=0.4, color=LINE,
        hAlign='CENTER', spaceAfter=10))
    # Erstellt am + optional fehlende Dokumente direkt darunter — gleiche Schrift
    _not_upl = d.get('not_uploaded', '')
    _show_warn = bool(_not_upl and 'Alle Pflichtdokumente' not in _not_upl)
    S.append(Paragraph(
        f"Erstellt am {d.get('datum','')}  ·  AeroTAX  ·  aerosteuer.de",
        ps("cf", fontSize=8, textColor=TEXT3, fontName="Helvetica",
           leading=12, alignment=TA_CENTER, spaceAfter=0)))
    if _show_warn:
        S.append(Paragraph(
            f'Fehlende Dokumente: {_not_upl}',
            ps('warn_miss', fontSize=8, textColor=TEXT3,
               fontName='Helvetica', leading=12, alignment=TA_CENTER)))
    # SEITE 2 — Anleitung: was der User in WISO tut
    # ════════════════════════════════════════════════
    S.append(PageBreak())
    S.append(Spacer(1, 0.4*cm))

    # ── Eyebrow + Erklärender Titel ──
    S.append(Paragraph("DEINE NÄCHSTEN SCHRITTE",
        ps("hero_eye", fontSize=8.5, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=12, spaceAfter=8, letterSpacing=2.5)))
    S.append(Paragraph("So trägst du das Ergebnis in WISO ein",
        ps("hero_h", fontSize=18, textColor=TEXT, fontName="Helvetica",
           leading=24, spaceAfter=6, letterSpacing=-0.2)))
    S.append(Paragraph(
        "Eine Eingabe, ein Wert — die Aufteilung liegt als Anlage bei. "
        "Folge den vier Schritten unten, dann ist deine Werbungskosten-Auswertung "
        "in deiner Steuererklärung verbucht.",
        ps("hero_sub", fontSize=10, textColor=TEXT2, fontName="Helvetica",
           leading=15, spaceAfter=24)))

    # ── Dezenter Betrag-Block (klein, elegant) ──
    betrag_box = Table([[
        Paragraph("EINZUTRAGENDER GESAMTBETRAG",
            ps("bb_l", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=11, letterSpacing=1.8)),
        Paragraph(eur(d['netto']),
            ps("bb_v", fontSize=20, textColor=TEXT, fontName="Helvetica",
               leading=24, alignment=TA_RIGHT, letterSpacing=-0.3)),
    ]], colWidths=[10.0*cm, 6.8*cm])
    betrag_box.setStyle(TableStyle([
        ("TOPPADDING",(0,0),(-1,-1),14),("BOTTOMPADDING",(0,0),(-1,-1),14),
        ("LEFTPADDING",(0,0),(-1,-1),16),("RIGHTPADDING",(0,0),(-1,-1),16),
        ("BACKGROUND",(0,0),(-1,-1), HexColor("#0a1224")),
        ("BOX",(0,0),(-1,-1), 0.6, LINE2),
        ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
    ]))
    try: betrag_box.cornerRadii = [10,10,10,10]
    except Exception: pass
    S.append(betrag_box)
    S.append(Spacer(1, 0.8*cm))

    # ── Anleitung-Section ──
    S.append(Paragraph("Schritt für Schritt in WISO",
        ps("steps_h", fontSize=12, textColor=TEXT2, fontName="Helvetica",
           leading=16, spaceAfter=14, letterSpacing=0.3)))

    # v8.3: Sachlicher WISO-Text — kein "Reisenebenkosten"/"alle anderen Felder
    # bleiben leer". Der Wert ist eine zusammengefasste Werbungskosten-Auswertung.
    steps = [
        ("1", "WISO Steuer öffnen",
         "Lege einen neuen Eintrag unter <b>Ausgaben → Werbungskosten → Reisekosten → Zusammengefasste Auswärtstätigkeiten</b> an."),
        ("2", "Beschreibung eintragen",
         f"Bei <i>Beschreibung der Auswärtstätigkeit</i> eintragen: <b>Werbungskosten-Auswertung AeroTAX {d.get('year', 2025)}</b>."),
        ("3", f"Gesamtbetrag eintragen:  <b>{eur(d['netto'])}</b>",
         "Trage den ausgewiesenen Gesamtbetrag ein. Der Wert ist eine "
         "<b>zusammengefasste Werbungskosten-Auswertung</b> aus Fahrtkosten, "
         "Reinigung, Reisenebenkosten und verbleibenden VMA nach Arbeitgeber-"
         "Erstattungen. Die genaue Aufteilung findest du im Abschnitt "
         "<i>Berechnung im Detail</i>."),
        ("4", "Dieses PDF als Anlage anhängen",
         "Füge dieses PDF deiner Steuererklärung bei. Es enthält Rechenweg, "
         "verwendete Werte und Nachweise. Bitte prüfe die Werte vor "
         "Übernahme in deine Steuersoftware."),
    ]
    for n, title, desc in steps:
        t = Table([[
            Paragraph(n, ps(f"sn{n}", fontSize=16, textColor=BLUE3,
                fontName="Helvetica", leading=20, alignment=TA_CENTER)),
            Paragraph(
                f"<b>{title}</b><br/>"
                f'<font color="#94a3b8" size="9">{desc}</font>',
                ps(f"sd{n}", fontSize=10.5, textColor=TEXT,
                   fontName="Helvetica", leading=15)),
        ]], colWidths=[1.1*cm, 15.7*cm])
        t.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),12),("BOTTOMPADDING",(0,0),(-1,-1),12),
            ("LEFTPADDING",(0,0),(-1,-1),10),("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEBELOW",(0,0),(-1,0),0.4,LINE),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
        ]))
        S.append(t)
    S.append(Spacer(1, 0.6*cm))

    # ── Optionale Belege — eigene klare Anleitung ──
    if belege:
        S.append(Spacer(1, 0.6*cm))
        S.append(HRFlowable(width="100%", thickness=0.4, color=LINE,
            spaceBefore=4, spaceAfter=22))
        S.append(Paragraph("ZUSÄTZLICH — DEINE OPTIONALEN BELEGE",
            ps("wak_eye", fontSize=8.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=12, spaceAfter=4, letterSpacing=2.5)))
        S.append(Paragraph("Diese Posten einzeln in WISO eintragen",
            ps("wak_h", fontSize=15, textColor=TEXT, fontName="Helvetica",
               leading=20, spaceAfter=6, letterSpacing=-0.2)))
        S.append(Paragraph(
            "Jeder Beleg gehört in einen eigenen WISO-Bereich. "
            "Lege pro Position einen neuen Eintrag an — Betrag und Pfad stehen jeweils dabei.",
            ps("wak_sub", fontSize=10, textColor=TEXT2, fontName="Helvetica",
               leading=15, spaceAfter=20)))

        for b in belege:
            has_doc = b.get('betrag', 0) > 0
            wiso_path = b.get('wiso', '') or 'Werbungskosten'
            # Card pro Beleg: Icon+Name links, Betrag mittig, WISO-Pfad rechts darunter
            head_row = Table([[
                Paragraph(
                    f"<b>{b.get('name','')}</b>",
                    ps(f"bn{id(b)}", fontSize=11,
                       textColor=TEXT if has_doc else TEXT3,
                       fontName="Helvetica", leading=15)),
                Paragraph(
                    f"<b>{eur(b['betrag'])}</b>" if has_doc else '<font color="#94a3b8">⚠ Beleg fehlt</font>',
                    ps(f"ba{id(b)}", fontSize=11,
                       textColor=TEXT if has_doc else TEXT3,
                       fontName="Helvetica", leading=15, alignment=TA_RIGHT)),
            ]], colWidths=[12.0*cm, 4.8*cm])
            head_row.setStyle(TableStyle([
                ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
                ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ]))
            S.append(head_row)
            # v8.5: WISO-Pfad mit auto-wrap (leading 15, kein Quetschen)
            S.append(Paragraph(
                f'<font color="#94a3b8">eintragen unter:</font>  <b>{wiso_path}</b>',
                ps(f"bw{id(b)}", fontSize=9, textColor=TEXT,
                   fontName="Helvetica", leading=15, spaceAfter=6,
                   wordWrap='CJK')))
            if has_doc:
                S.append(Paragraph(
                    'Vollen Beleg-Betrag eintragen — WISO berechnet den absetzbaren Anteil automatisch.',
                    ps(f"bh{id(b)}", fontSize=8.5, textColor=TEXT3,
                       fontName="Helvetica", leading=12, spaceAfter=2)))
            S.append(HRFlowable(width="100%", thickness=0.3, color=LINE,
                spaceBefore=8, spaceAfter=10))

    # ════════════════════════════════════════════════
    # TRENNSEITE — elegant, jahres- und beleg-agnostisch
    # ════════════════════════════════════════════════
    S.append(PageBreak())
    S.append(Spacer(1, 5*cm))
    S.append(Paragraph("ALL DOORS IN PARK",
        ps("sep0", fontSize=8.5, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=12, alignment=TA_CENTER, spaceAfter=20, letterSpacing=2.5)))
    S.append(Paragraph("Auswertung abgeschlossen.",
        ps("sep1", fontSize=22, textColor=TEXT, fontName="Helvetica",
           leading=28, alignment=TA_CENTER, spaceAfter=24, letterSpacing=-0.2)))
    S.append(HRFlowable(width="8%", thickness=0.5, color=LINE2,
        hAlign='CENTER', spaceAfter=24))
    S.append(Paragraph(
        "Deine Werbungskosten sind ausgewertet, der einzutragende Betrag steht fest. "
        "Was zu tun bleibt, ist Eintragen — die Anleitung dafür liegt im vorderen Teil dieses Dokuments.",
        ps("sepbody", fontSize=10.5, textColor=TEXT2, fontName="Helvetica",
           leading=18, alignment=TA_CENTER, spaceAfter=44)))

    S.append(HRFlowable(width="24%", thickness=0.4, color=LINE,
        hAlign='CENTER', spaceAfter=18))
    S.append(Paragraph("Ab hier nur zur Information",
        ps("sep2", fontSize=9, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=13, alignment=TA_CENTER, spaceAfter=14, letterSpacing=1.8)))
    S.append(Paragraph(
        "Die folgenden Seiten dienen als Nachweis und Begleit-Dokumentation deiner Auswertung: "
        "die detaillierte Berechnung nach den jeweils gültigen BMF-Pauschalen, "
        "alle hochgeladenen Belege als Anlagen sowie die Bestätigungsseite zur Unterschrift.",
        ps("sep3", fontSize=9.5, textColor=TEXT2, fontName="Helvetica",
           leading=16, alignment=TA_CENTER, spaceAfter=12)))
    S.append(Paragraph(
        "Sollte das Finanzamt Rückfragen haben, findest du hier alles "
        "<i>geordnet, beschriftet und nachvollziehbar</i> — Seite für Seite.",
        ps("sep4", fontSize=9.5, textColor=TEXT3, fontName="Helvetica",
           leading=15, alignment=TA_CENTER)))

    # Rechtlicher Disclaimer
    S.append(Spacer(1, 1.2*cm))
    S.append(HRFlowable(width="40%", thickness=0.3, color=LINE,
        hAlign='CENTER', spaceAfter=10))
    S.append(Paragraph("Rechtlicher Hinweis",
        ps("disc_t", fontSize=8.5, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=12, alignment=TA_CENTER, spaceAfter=8, letterSpacing=1.5)))
    S.append(Paragraph(
        "Diese Auswertung wurde automatisiert auf Basis deiner hochgeladenen "
        "Dokumente erstellt. AeroTAX ist ein Berechnungs- und "
        "Dokumentationswerkzeug und ersetzt keine individuelle steuerliche "
        "Beratung. Bitte prüfe die Werte vor Übernahme in deine Steuersoftware. "
        "Bei steuerlichen Fragen wende dich an eine zugelassene Steuerberaterin, "
        "einen Steuerberater oder deine Steuersoftware.",
        ps("disc_b", fontSize=8.5, textColor=TEXT3, fontName="Helvetica",
           leading=13, alignment=TA_CENTER, spaceAfter=8)))

    # ════════════════════════════════════════════════
    # TAG-FÜR-TAG-NACHWEIS (Audit) — nur wenn tage_detail vorhanden
    # ════════════════════════════════════════════════
    tage_detail = d.get('_tage_detail') or []
    if tage_detail and isinstance(tage_detail, list):
        S.append(PageBreak())
        for el in section("Tag-für-Tag-Nachweis"): S.append(el)
        S.append(Paragraph(
            "Diese Tabelle dokumentiert wie jeder dienstliche Tag klassifiziert wurde. "
            "Sie dient als Nachweis gegenüber dem Finanzamt und Steuerberater.",
            ps("td_intro", fontSize=9, textColor=TEXT2, fontName="Helvetica",
               leading=13, alignment=TA_LEFT, spaceAfter=12)))
        # v10: Cells als Paragraph mit wordWrap='CJK' — lange Routing-/Begründung-Strings
        # wickeln in die nächste Zeile statt rechts aus der Spalte zu fließen.
        # ReportLab's ps() ist `ParagraphStyle(name, parent=Normal, **kw)` — wordWrap durchgereicht.
        def _safe_cell(s):
            # Escape für Paragraph (HTML-Parser von ReportLab interpretiert <, >, &).
            return (str(s or '')
                    .replace('&', '&amp;')
                    .replace('<', '&lt;')
                    .replace('>', '&gt;'))
        cell_head_style = ps('td_head', fontSize=7.5, leading=9.5, fontName='Helvetica-Bold',
                             textColor=TEXT, wordWrap='CJK', alignment=TA_LEFT)
        cell_body_style = ps('td_body', fontSize=6.8, leading=8.6, fontName='Helvetica',
                             textColor=TEXT2, wordWrap='CJK', alignment=TA_LEFT)
        tdata = [[
            Paragraph('Datum', cell_head_style),
            Paragraph('Marker', cell_head_style),
            Paragraph('Routing', cell_head_style),
            Paragraph('Klass.', cell_head_style),
            Paragraph('Begründung', cell_head_style),
        ]]
        # v8.18.6: Cap auf 366 (volles Jahr inkl. Schaltjahr) — vorher 200 → Cut-off Mitte Juli
        for entry in tage_detail[:366]:
            if not isinstance(entry, dict):
                continue
            datum = _safe_cell(entry.get('datum', ''))[:10]
            # v10: Marker/Routing/Klass/Begründung NICHT mehr hart truncaten —
            # wordWrap='CJK' fließt in die nächste Zeile innerhalb der Zelle.
            marker = _safe_cell(entry.get('marker', ''))[:40]
            routing = _safe_cell(entry.get('routing', ''))[:80]
            klass = _safe_cell(entry.get('klass', ''))[:12]
            begr = _safe_cell(entry.get('begruendung', ''))[:240]
            tdata.append([
                Paragraph(datum, cell_body_style),
                Paragraph(marker, cell_body_style),
                Paragraph(routing, cell_body_style),
                Paragraph(klass, cell_body_style),
                Paragraph(begr, cell_body_style),
            ])
        if len(tdata) > 1:
            # v10: Routing-Spalte etwas breiter (lange Flugnummer+Routing wie 'LH0400 A FRA 0 FRA-JFK')
            ttab = LongTable(tdata, colWidths=[1.7*cm, 1.7*cm, 2.6*cm, 1.4*cm, 8.9*cm], repeatRows=1)
            ttab.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), BG_CARD),
                # FONTNAME/FONTSIZE/TEXTCOLOR werden von Paragraph-Style getragen,
                # bleiben hier als Defensiv-Fallback falls eine Zelle als String durchschlüpft.
                ('FONTSIZE', (0,0), (-1,-1), 7.0),
                ('VALIGN', (0,0), (-1,-1), 'TOP'),
                ('LINEBELOW', (0,0), (-1,0), 0.4, LINE2),
                ('LINEBELOW', (0,1), (-1,-1), 0.2, LINE),
                ('LEFTPADDING', (0,0), (-1,-1), 4),
                ('RIGHTPADDING', (0,0), (-1,-1), 4),
                ('TOPPADDING', (0,0), (-1,-1), 3),
                ('BOTTOMPADDING', (0,0), (-1,-1), 3),
            ]))
            S.append(ttab)
            S.append(Spacer(1, 0.4*cm))
            unklare = d.get('_unklare_tage') or []
            if unklare:
                S.append(Paragraph(
                    f"<b>Unklare Tage ({len(unklare)}):</b> Diese Tage konnten nicht eindeutig klassifiziert werden — bitte selbst prüfen.",
                    ps("td_unklar", fontSize=8.5, textColor=GOLD, fontName="Helvetica",
                       leading=12, alignment=TA_LEFT, spaceAfter=6)))
                for u in unklare[:15]:
                    S.append(Paragraph(f"• {str(u)[:200]}",
                        ps(f"td_u{id(u)}", fontSize=8, textColor=TEXT3,
                           fontName="Helvetica", leading=11, leftIndent=10, spaceAfter=2)))

    # ════════════════════════════════════════════════
    # BELEGE — nur wenn Fotos vorhanden
    # ════════════════════════════════════════════════
    # Belege page — always shown
    S.append(PageBreak())
    for el in section("Belege — Hochgeladene Dokumente"): S.append(el)
    if not has_fotos:
        S.append(Spacer(1, 1.5*cm))
        S.append(Paragraph("Keine Belege hochgeladen.",
            ps("no_belege", fontSize=11, textColor=TEXT2,
               fontName="Helvetica", leading=16, alignment=TA_CENTER,
               spaceAfter=8)))
        S.append(Paragraph(
            "Es wurden keine Belege hochgeladen. Lade beim nächsten Mal deine Rechnungen unter Schritt 2 hoch — dann musst du sie nicht manuell in WISO suchen.",
            ps("no_belege_sub", fontSize=9, textColor=TEXT3,
               fontName="Helvetica", leading=14, alignment=TA_CENTER)))
    if has_fotos:
      W_c = A4[0] - 3.2*cm
      first = True
      for b in belege:
            fbl = b.get('file_bytes_list') or []
            if not fbl: continue
            betrag = b.get('betrag', 0)
            for fidx, fb_item in enumerate(fbl):
                # Normalisieren: kann bytes oder (bytes, filename) sein
                fb = fb_item[0] if isinstance(fb_item, tuple) else fb_item
                if not first: S.append(PageBreak())
                first = False
                S.append(Paragraph(f"{b.get('name','')}",
                    ps(f"bpn{id(b)}{fidx}", fontSize=11, textColor=TEXT,
                       fontName="Helvetica-Bold", leading=15, spaceAfter=4)))
                S.append(Paragraph(
                    f"Betrag: {eur(betrag)}" if betrag>0 else "— Betrag nicht erkannt —",
                    ps(f"bpp{id(b)}{fidx}", fontSize=8.5, textColor=TEXT2,
                       fontName="Helvetica", leading=12, spaceAfter=10)))
                S.append(hr(0, 12))
                try:
                    # HEIC (iPhone) ODER große Bilder → erst auf max 1500px skalieren
                    # spart massiv RAM (12MP-iPhone-Foto: 100MB → 5MB) ohne PDF-Qualitätsverlust
                    is_heic = b'ftypheic' in fb[:32] or b'ftypheix' in fb[:32] or b'ftypmif1' in fb[:32]
                    is_image = (is_heic or fb[:3]==b'\xff\xd8\xff' or fb[:4]==b'\x89PNG' or
                                fb[:6]==b'GIF87a' or fb[:6]==b'GIF89a' or fb[8:12]==b'WEBP')
                    if is_image and PIL_AVAILABLE:
                        src_img = None
                        try:
                            from PIL import Image as PILImage
                            src_img = PILImage.open(io.BytesIO(fb))
                            max_dim = 1500
                            if max(src_img.size) > max_dim:
                                ratio = max_dim / max(src_img.size)
                                new_size = (int(src_img.size[0]*ratio), int(src_img.size[1]*ratio))
                                src_img = src_img.resize(new_size, PILImage.LANCZOS)
                                print(f"[img-scale] {b.get('name','?')}: → {new_size[0]}×{new_size[1]}")
                            buf_jpg = io.BytesIO()
                            src_img.convert('RGB').save(buf_jpg, format='JPEG', quality=82, optimize=True)
                            fb = buf_jpg.getvalue()
                        except Exception as _hc:
                            print(f"[img-scale] fail: {_hc}")
                        finally:
                            try:
                                if src_img is not None:
                                    src_img.close()
                            except: pass
                    if fb[:3]==b'\xff\xd8\xff' or fb[:4]==b'\x89PNG' or fb[:6]==b'GIF87a' or fb[:6]==b'GIF89a' or fb[8:12]==b'WEBP':
                        img = RLImage(io.BytesIO(fb))
                        iw,ih = img.drawWidth,img.drawHeight
                        if iw and ih:
                            scale = min(W_c/iw, 20*cm/ih, 1.0)  # 20cm cap (war 22cm) wegen Header/Footer
                            img.drawWidth=iw*scale; img.drawHeight=ih*scale
                            S.append(img)
                        else:
                            S.append(Paragraph("⚠ Bild konnte nicht eingebettet werden (unbekannte Dimensionen).",
                                ps(f"bpe{id(b)}{fidx}", fontSize=9, textColor=TEXT3, fontName="Helvetica")))
                    else:
                        with pdfplumber.open(io.BytesIO(fb)) as pdoc:
                            for pgi,pg_ in enumerate(pdoc.pages):
                                if pgi>0: S.append(PageBreak())
                                for line in (pg_.extract_text() or '').split('\n'):
                                    if line.strip():
                                        S.append(Paragraph(
                                            line.replace('&','&amp;').replace('<','&lt;').replace('>','&gt;'),
                                            ps(f"pl{id(b)}{pgi}{id(line)}", fontSize=8.5,
                                               textColor=TEXT, fontName="Courier", leading=12)))
                                    else:
                                        S.append(Spacer(1, 0.1*cm))
                except Exception as embed_err:
                    print(f"PDF-Embed fail [{b.get('name','?')}/{fidx}, {len(fb)}B, magic={fb[:8].hex()}]: {type(embed_err).__name__}: {embed_err}")
                    S.append(Paragraph("Datei konnte nicht eingebettet werden.",
                        ps(f"fe{id(b)}{fidx}", fontSize=9, textColor=TEXT3,
                           fontName="Helvetica", leading=12)))

    # ════════════════════════════════════════════════
    # BERECHNUNG — minimalistisch auf Dark Navy
    # ════════════════════════════════════════════════
    S.append(PageBreak())
    for el in section("Berechnung — Zur Information"): S.append(el)

    # Jahres-spezifische Pauschalen für PDF-Anzeige
    _yr = int(d.get('year', 2025) or 2025)
    _bmf = BMF_INLAND_BY_YEAR.get(_yr, BMF_INLAND_BY_YEAR[2025])
    _rsatz = REINIGUNG_PRO_TAG_BY_YEAR.get(_yr, 1.60)
    _tsatz = TRINKGELD_PRO_NACHT_BY_YEAR.get(_yr, 3.60)
    _de_dec = lambda f: f"{f:.2f}".replace('.', ',')
    calc_items = [
        (f"Fahrtkosten Homebase  ({d.get('km',0)} km × {d.get('fahr_tage',0)} Tage)", "Zeilen 27–30", eur(d.get('fahr',0))),
        (f"Reinigungskosten  ({d.get('reinigungstage', d.get('arbeitstage',0))} Reinigungstage × {_de_dec(_rsatz)} €)", "Zeile 62", eur(d.get('reinig',0))),
        (f"Trinkgelder  ({d.get('hotel_naechte',0)} Nächte × {_de_dec(_tsatz)} €)", "Zeile 68", eur(d.get('trink',0))),
        (f"VMA Inland >8h  ({d.get('vma_72_tage',0)} Tage × {_de_dec(_bmf['tagestrip_8h'])} €)", "Zeile 72", eur(d.get('vma_72',0))),
        (f"VMA An-/Abreisetage  ({d.get('vma_73_tage',0)} Tage × {_de_dec(_bmf['an_abreise'])} €)", "Zeile 73", eur(d.get('vma_73',0))),
        (f"VMA 24h  ({d.get('vma_74_tage',0)} Tage × {_de_dec(_bmf['voll_24h'])} €)", "Zeile 74", eur(d.get('vma_74',0))),
        (f"VMA Ausland nach BMF-Pauschalen {_yr}", "Zeile 76", eur(d.get('vma_aus',0))),
    ]
    for label, zeile, val in calc_items:
        t = Table([[
            Paragraph(label, ps(f"cl{id(label)}", fontSize=9, textColor=TEXT2,
                fontName="Helvetica", leading=12)),
            Paragraph(zeile, ps(f"cz{id(zeile)}", fontSize=8, textColor=TEXT3,
                fontName="Helvetica", leading=12, alignment=TA_CENTER)),
            Paragraph(val, ps(f"cv{id(val)}", fontSize=9, textColor=TEXT,
                fontName="Helvetica", leading=12, alignment=TA_RIGHT)),
        ]], colWidths=[10.5*cm, 2*cm, 4.3*cm])
        t.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEBELOW",(0,0),(-1,0),0.3,LINE),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
        ]))
        S.append(t)

    S.append(kv_total("Summe aller Aufwendungen", eur(d.get('gesamt',0))))
    S.append(kv(f"Abzug: AG-Fahrkostenzuschuss (Z17)",
        eur(-d.get('ag_z17',0)), vc=TEXT2))
    S.append(kv(f"Abzug: Steuerfreie Spesen Lufthansa (Z77)",
        eur(-d.get('z77',0)), vc=TEXT2))
    S.append(kv_total("Einzutragender Betrag", eur(d.get('netto',0))))
    S.append(Spacer(1, 0.5*cm))

    # Monate
    abrechnungen = d.get('abrechnungen', [])
    if abrechnungen:
        S.append(Paragraph("Streckeneinsatz-Abrechnungen",
            ps("se", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=11, spaceBefore=8, spaceAfter=10, letterSpacing=1.5)))
        for a in abrechnungen:
            t = Table([[
                Paragraph(a.get('bezeichnung',''), ps(f"mn{id(a)}", fontSize=9,
                    textColor=TEXT2, fontName="Helvetica", leading=12)),
                Paragraph(a.get('erstellt',''), ps(f"me{id(a)}", fontSize=8,
                    textColor=TEXT3, fontName="Helvetica", leading=12)),
                Paragraph(eur(a.get('gesamt',0)), ps(f"mg{id(a)}", fontSize=9,
                    textColor=TEXT, fontName="Helvetica", leading=12, alignment=TA_RIGHT)),
                Paragraph(eur(a.get('steuerfrei',0)), ps(f"ms{id(a)}", fontSize=9,
                    textColor=TEXT, fontName="Helvetica", leading=12, alignment=TA_RIGHT)),
                Paragraph(eur(a.get('steuerpflichtig',0)), ps(f"mp{id(a)}", fontSize=9,
                    textColor=TEXT2, fontName="Helvetica", leading=12, alignment=TA_RIGHT)),
            ]], colWidths=[3.5*cm, 2.8*cm, 2.9*cm, 4.0*cm, 3.6*cm])
            t.setStyle(TableStyle([
                ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
                ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                ("LINEBELOW",(0,0),(-1,0),0.3,LINE),
                ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ]))
            S.append(t)
        S.append(kv_total("Gesamt Steuerfrei (= Z77)", eur(d.get('z77',0))))

    # ════════════════════════════════════════════════
    # SONDERAUSGABEN & LSB-ÜBERSICHT
    # ════════════════════════════════════════════════
    brutto    = d.get('brutto', 0)
    lohnst    = d.get('lohnsteuer', 0)
    soli      = d.get('soli', 0)
    kirche    = d.get('kirchensteuer', 0)
    rv_an     = d.get('rv_an', 0)
    rv_ag     = d.get('rv_ag', 0)
    rv_ges    = d.get('rv_gesamt', 0)
    kv_an     = d.get('kv_an', 0)
    pv_an     = d.get('pv_an', 0)
    av_an     = d.get('av_an', 0)
    vorsorge  = d.get('vorsorge_gesamt_an', 0)
    sk        = d.get('steuerklasse', '1')
    kfb       = d.get('kinderfreibetraege', 0)
    identnr   = d.get('identnr', '')
    gebdat    = d.get('geburtsdatum', '')
    pnr       = d.get('personalnummer', '')

    if brutto > 0:
        S.append(PageBreak())
        for el in section("Lohnsteuerbescheinigung — Übersicht"): S.append(el)

        # v8.5: LSB-Seite radikal vereinfacht. Nur AeroTAX-relevante Werte
        # (Brutto, Lohnsteuer, Soli, KSt, Z17). Vorsorgeaufwendungen,
        # Sozialversicherungs-Details, WO-IN-WISO-Block und PII raus —
        # die übernimmt WISO automatisch oder der User selbst aus seiner LSB.
        S.append(Paragraph(
            "Für die AeroTAX-Berechnung verwendete Werte:",
            ps("lsb_intro", fontSize=9.5, textColor=TEXT2, fontName="Helvetica",
               leading=14, spaceAfter=14)))

        z17_label_long = "AG-Fahrkostenzuschuss Z17 (→ Abzug Fahrtkosten-/Anreisekosten-Topf)"
        lsb_items = [
            ("Bruttoarbeitslohn (Zeile 3)", eur(brutto)),
            ("Einbehaltene Lohnsteuer (Zeile 4)", eur(lohnst)),
            ("Solidaritätszuschlag (Zeile 5)", eur(soli)),
            ("Kirchensteuer AN (Zeile 6)", eur(kirche)),
            (z17_label_long, eur(d.get('ag_z17', 0))),
        ]
        for label, val in lsb_items:
            S.append(kv(label, val))
        S.append(Spacer(1, 0.4*cm))

        # Z17-Hinweis sachlich
        z17_hint = "Hinweis: Z17 wird ausschließlich mit dem Fahrtkosten-/Anreisekosten-Topf verrechnet."
        S.append(Paragraph(z17_hint,
            ps("lsb_z17_note", fontSize=9, textColor=TEXT2, fontName="Helvetica",
               leading=14, spaceAfter=10)))

        # Optional: Z20-Hinweis falls erkannt
        z20 = d.get('verpfl_z20', 0) or 0
        if z20 and float(z20) > 0:
            S.append(Paragraph(
                f"Steuerfreie Verpflegungs-/Reisekosten laut Lohnsteuer­"
                f"bescheinigung (Z20): <b>{eur(z20)}</b>",
                ps("lsb_z20", fontSize=9.5, textColor=TEXT, fontName="Helvetica",
                   leading=14, spaceBefore=6)))
            S.append(Paragraph(
                "Hinweis: AeroTAX verwendet für die VMA-Verrechnung "
                "vorrangig die Streckeneinsatzabrechnung, damit keine "
                "Doppelzählung entsteht.",
                ps("lsb_z20_note", fontSize=9, textColor=TEXT2,
                   fontName="Helvetica", leading=14, spaceAfter=4)))


    # Audit-Trail / Verifikations-Status komplett raus —
    # User will nur die AeroTAX-Auswertung, keine KI-/Methodik-Hinweise im PDF.
    S.append(PageBreak())
    for el in section("Bestätigung & Unterschrift"): S.append(el)

    S.append(Paragraph(
        "Ich bestätige, dass ich alle Angaben in diesem Dokument "
        "persönlich geprüft habe und diese nach meiner Kenntnis "
        "vollständig und korrekt sind. Mir ist bewusst, dass ich "
        "als Steuerpflichtiger für die Richtigkeit meiner "
        "Steuererklärung gegenüber dem Finanzamt verantwortlich bin.",
        ps("conf", fontSize=9.5, textColor=TEXT, fontName="Helvetica",
           leading=17, spaceAfter=36)))

    for label, value in [("Name", d.get('name','')), ("Datum", d.get('datum',''))]:
        S.append(Paragraph(label,
            ps(f"sl{label}", fontSize=7.5, textColor=TEXT3,
               fontName="Helvetica-Bold", leading=11,
               spaceAfter=4, letterSpacing=1.5)))
        S.append(Paragraph(value,
            ps(f"sv{label}", fontSize=11, textColor=TEXT,
               fontName="Helvetica", leading=14, spaceAfter=22)))

    S.append(Paragraph("Unterschrift",
        ps("sig_l", fontSize=7.5, textColor=TEXT3,
           fontName="Helvetica-Bold", leading=11,
           spaceAfter=10, letterSpacing=1.5)))
    sig = Table([[""]], colWidths=[16.8*cm], rowHeights=[4.2*cm])
    sig.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,-1), WHITE),
        ("BOX",(0,0),(-1,-1), 0.6, LINE2),
    ]))
    S.append(sig)
    S.append(Spacer(1, 0.8*cm))
    S.append(hr(0, 10))
    S.append(Paragraph(
        f"AeroTAX  ·  aerosteuer.de  ·  Erstellt am {d.get('datum','')}",
        ps("ff", fontSize=7.5, textColor=TEXT3, fontName="Helvetica",
           leading=11, alignment=TA_CENTER)))

    doc.build(S, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()
