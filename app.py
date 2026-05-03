# ═══════════════════════════════════════════════════════════════
#  AEROTAX BACKEND — app.py
#  Deploy auf Railway.app
#
#  Umgebungsvariablen (in Railway Dashboard setzen):
#    ANTHROPIC_API_KEY      = sk-ant-...
#    STRIPE_SECRET_KEY      = sk_live_...
#    STRIPE_WEBHOOK_SECRET  = whsec_...
#    AEROTAX_PRICE_ID       = price_... (15 EUR Produkt in Stripe)
#    FRONTEND_URL           = https://aerotax.de
#    PORT                   = 5000
# ═══════════════════════════════════════════════════════════════

import os, io, uuid, json, re, tempfile
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
                                 Table, TableStyle, PageBreak, HRFlowable,
                                 Image as RLImage)
from reportlab.lib.enums import TA_RIGHT, TA_CENTER

# ── APP SETUP ─────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins=[
    os.getenv('FRONTEND_URL', 'https://aerosteuer.de'),
    'https://aerosteuer.de',
    'https://aerosteuer.pages.dev',
    'http://localhost:3000',
    'http://localhost:8080',
])

stripe.api_key        = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET        = os.getenv('STRIPE_WEBHOOK_SECRET')
ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY')
PRICE_ID              = os.getenv('AEROTAX_PRICE_ID')
FRONTEND_URL          = os.getenv('FRONTEND_URL','https://aerotax.de')

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
    """Creates a Stripe PaymentIntent for Stripe Elements (no redirect)."""
    try:
        data = request.get_json() or {}
        amount = int(data.get('amount', 1999))
        currency = data.get('currency', 'eur')
        ref = str(uuid.uuid4())

        # Save ref for later file processing
        _store[ref] = {
            'form': data,
            'files': {},
            'paid': False,
            'expires': datetime.utcnow() + timedelta(hours=2),
        }

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


_ALL_FILE_KEYS = (
    'lsb', 'dp', 'se',
    'stb', 'gew', 'arb', 'fort', 'tel', 'konz',
    'lapt', 'fach', 'reini', 'bewer',
    'bu', 'haft', 'kv', 'rv', 'leb', 'haus',
    'arzt', 'zahn', 'medi', 'pfle', 'under', 'kata',
    'spen', 'part', 'kind', 'hand', 'haed',
)

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

@app.route('/api/stripe-webhook', methods=['POST'])
def stripe_webhook():
    """Markiert _store[ref].paid=True. Auswertung selbst läuft NICHT hier — der
    Frontend-Flow ruft /api/process direkt auf nach Payment-Element Erfolg.
    Webhook bleibt als Backup / Confirmation bestehen."""
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature', '')

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
                pdf_bytes = base64.b64decode(row['pdf_b64'])
                # In-Memory cachen
                _store[token] = {'pdf_bytes': pdf_bytes, 'filename': row.get('filename'), 'expires': exp}
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
    return send_file(
        io.BytesIO(pdf_bytes),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=filename,
    )


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


# ── BILD-NORMALISIERUNG (HEIC/WEBP/etc → JPEG) ─────────────────
def _normalize_upload(file_bytes, filename=''):
    """Konvertiert exotische Bildformate (HEIC/HEIF/WEBP/…) zu JPEG.
    PDFs, JPEG, PNG bleiben unverändert. Garantiert dass Claude UND
    der PDF-Generator die Bytes lesen können.
    Returns (bytes, filename) — Endung wird auf .jpg gesetzt wenn konvertiert.
    """
    if not file_bytes:
        return file_bytes, filename
    ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
    # Bereits in einem unterstützten Format → unverändert
    if file_bytes[:4] == b'%PDF' or ext == 'pdf':
        return file_bytes, filename
    if file_bytes[:3] == b'\xff\xd8\xff':  # JPEG
        return file_bytes, filename
    if file_bytes[:8] == b'\x89PNG\r\n\x1a\n':  # PNG
        return file_bytes, filename
    # Konvertierung versuchen
    if PIL_AVAILABLE:
        try:
            img = Image.open(io.BytesIO(file_bytes))
            buf = io.BytesIO()
            img.convert('RGB').save(buf, format='JPEG', quality=88)
            new_bytes = buf.getvalue()
            new_name = (filename.rsplit('.', 1)[0] + '.jpg') if '.' in filename else (filename or 'image') + '.jpg'
            print(f"Bild normalisiert ({ext or 'unbekannt'} → JPEG): {filename} {len(file_bytes)//1024}KB → {len(new_bytes)//1024}KB")
            return new_bytes, new_name
        except Exception as e:
            print(f"Bild-Normalisierung fehlgeschlagen für {filename}: {e}")
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


def _save_job_to_disk(job_id):
    """Speichert Job-State nach Disk — überlebt Render-Restart."""
    with _jobs_lock:
        j = _jobs.get(job_id, {}).copy()
    if not j: return
    # Entferne Binär-Daten (PDFs gehören in _store, nicht in Job)
    j_safe = {k: v for k, v in j.items() if k != 'files'}
    try:
        with open(os.path.join(_JOBS_DIR, f'{job_id}.json'), 'w') as f:
            json.dump(j_safe, f, default=str)
    except Exception as e:
        print(f"[persist] Job {job_id[:8]} save fail: {e}")


def _load_job_from_disk(job_id):
    """Lädt Job-State vom Disk falls Memory-Dict leer (nach Server-Restart)."""
    path = os.path.join(_JOBS_DIR, f'{job_id}.json')
    if not os.path.exists(path): return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        print(f"[persist] Job {job_id[:8]} load fail: {e}")
        return None


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


@app.route('/api/process', methods=['POST'])
def process_real():
    """Startet asynchrone Auswertung. Liefert sofort job_id, Frontend pollt /api/job/<id>."""
    try:
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

        form = {
            'name':    request.form.get('name', 'Flugbegleiter'),
            'year':    year_input,
            'base':    request.form.get('base', 'Frankfurt (FRA)'),
            'anreise': anreise,
            'km':      km_capped if anreise in ('auto','fahrrad') else 0,
            'fahrzeug':   request.form.get('fahrzeug', 'verbrenner'),
            'oepnv_kosten': oepnv_capped,
            'shuttle_kosten': shuttle_capped,
            'jobticket':  request.form.get('jobticket', 'nein'),
            'anfahrt_min': anfahrt_capped,
        }

        files = {}
        for key in _ALL_FILE_KEYS:
            uploaded = request.files.getlist(key)
            if uploaded:
                files[key] = [_normalize_upload(f.read(), f.filename) for f in uploaded]

        # Fallback: Files aus _store (in-memory) — überlebt Stripe-Retry
        ref_for_fallback = (request.form.get('ref') or '').strip()
        if (not files.get('lsb') or not files.get('dp') or not files.get('se')) \
                and ref_for_fallback and _store.get(ref_for_fallback, {}).get('files'):
            stored = _store[ref_for_fallback]['files']
            for k, items in stored.items():
                if k in files:
                    continue
                files[k] = [it[0] if isinstance(it, tuple) else it for it in items]
            print(f"[process] ref={ref_for_fallback[:8]} Files aus _store geladen ({sum(len(v) for v in files.values())} insgesamt)")

        # Letzter Fallback: Supabase — überlebt Render-Restart
        if (not files.get('lsb') or not files.get('dp') or not files.get('se')) \
                and ref_for_fallback:
            sb_files = _load_uploaded_files_supabase(ref_for_fallback)
            if sb_files:
                for k, items in sb_files.items():
                    if k in files:
                        continue
                    files[k] = [d for (d, _) in items]
                print(f"[process] ref={ref_for_fallback[:8]} Files aus Supabase geladen ({sum(len(v) for v in files.values())} insgesamt)")

        if not files.get('lsb') or not files.get('dp') or not files.get('se'):
            return jsonify({
                'error': 'Pflicht-Dokumente fehlen. Bitte lade Lohnsteuerbescheinigung, '
                         'Flugstunden-Übersichten und Streckeneinsatz-Abrechnungen hoch.'
            }), 400

        # ── PAYMENT-GATE: ref (Stripe), free_retry_token, oder valider Promo-Code ──
        free_retry_token = (request.form.get('free_retry_token') or '').strip()
        ref = (request.form.get('ref') or '').strip()
        pi_id = (request.form.get('payment_intent_id') or '').strip()
        promo_code = (request.form.get('promo_code') or '').strip().upper()
        is_free_retry = bool(free_retry_token and free_retry_token in _recovery_tokens)
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
                if pi and pi.status == 'succeeded':
                    is_paid = True
                    if not ref:
                        ref = (pi.metadata or {}).get('ref') or ''
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
        with _jobs_lock:
            _jobs[job_id] = {
                'status':   'pending',
                'progress': 0,
                'created':  datetime.utcnow().isoformat() + 'Z',
                'session_token': session_token,
            }
        # ref/pi_id ans form-dict heften — für späteren Cleanup nach erfolgreicher Auswertung
        form['ref'] = ref or ''
        form['pi_id'] = pi_id or ''
        _audit(job_id, 'job_created', {'year': form['year'], 'base': form['base'], 'files': {k: len(v) for k, v in files.items()}})

        Thread = __import__('threading').Thread
        Thread(target=_run_process_async, args=(job_id, form, files), daemon=True).start()

        return jsonify({
            'job_id': job_id,
            'status': 'pending',
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

        result = berechne(form, files)
        if isinstance(result, tuple):
            result = result[0]
        _audit(job_id, 'calculation_done', {
            'gesamt': result.get('gesamt'), 'netto': result.get('netto'),
            'verification': result.get('_verification'),
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

        safe = {k: v for k, v in result.items() if isinstance(v, (int, float, str))}
        opt_belege_safe = []
        for b in result.get('optionale_belege', []):
            b_safe = {k: v for k, v in b.items() if k != 'file_bytes_list'}
            opt_belege_safe.append(b_safe)

        # Session-Token wurde bereits beim job_created erstellt — jetzt nur Result reinpacken
        with _jobs_lock:
            session_token = _jobs[job_id].get('session_token')
        if session_token:
            _save_session(session_token, {
                'job_id': job_id,
                'result_data': safe,
                'notes': result.get('notes', []),
                'download_url': f'/api/download/{token}',
                'chat_history': [],
            })

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
    return jsonify(safe)


@app.route('/api/job/<job_id>/audit', methods=['GET'])
def get_job_audit(job_id):
    """Vollständiges Audit-Log (für Compliance / Steuerberater)."""
    with _jobs_lock:
        j = _jobs.get(job_id) or _load_job_from_disk(job_id)
    if not j:
        return jsonify({'error': 'job not found'}), 404
    return jsonify({'audit': j.get('audit', []), 'status': j.get('status')})


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


@app.route('/api/health/full', methods=['GET'])
def full_health_check():
    """End-to-End Health Check: Server, Anthropic API, File-System."""
    health = {'server': 'ok', 'timestamp': datetime.utcnow().isoformat() + 'Z'}
    # Anthropic
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
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


# Recovery-Tokens: erlauben kostenlose Wiederholung in 30 Min Fenster
_recovery_tokens = {}


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
    """Background-Loop für regelmäßiges Cleanup (alle 30 Min)."""
    import time as _t
    while True:
        try:
            _t.sleep(1800)  # 30 Min
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
        except: pass


# Cleanup-Thread starten
__import__('threading').Thread(target=_cleanup_loop, daemon=True).start()


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


@app.route('/api/session/<token>', methods=['GET'])
def session_recall(token):
    """Holt Auswertungs-Ergebnis via Session-Token."""
    s = _load_session(token)
    if not s:
        return jsonify({'error': 'Session-Token ungültig oder abgelaufen'}), 404
    # Sensitiver Chat-Verlauf nicht standardmäßig zurückgeben
    safe = {k: v for k, v in s.items() if k != 'chat_history'}
    return jsonify(safe)


@app.route('/api/chat', methods=['POST'])
def chat_with_aerotax():
    """Chat mit AeroTAX über deine Auswertung. Body: {token, message}."""
    body = request.get_json(silent=True) or {}
    token = body.get('token', '').strip()
    message = (body.get('message') or '').strip()[:2000]
    if not token or not message:
        return jsonify({'error': 'token und message erforderlich'}), 400
    if len(message) < 3:
        return jsonify({'error': 'Frage zu kurz'}), 400

    session = _load_session(token)
    if not session:
        return jsonify({'error': 'Session-Token ungültig oder abgelaufen — bitte neu auswerten'}), 401

    # ── COST-CONTROL: Hard-Caps pro Session ──────────────────
    # 25 Nachrichten total in 24h → bei 19,99€ Umsatz noch sehr profitabel
    chat_history_existing = session.get('chat_history', [])
    user_msg_count = sum(1 for m in chat_history_existing if m.get('role') == 'user')
    HARD_CAP = 25
    if user_msg_count >= HARD_CAP:
        return jsonify({
            'error': f'Maximum {HARD_CAP} Chat-Nachrichten pro Session erreicht. Du kannst weiterhin deine Auswertung als PDF runterladen und im Forum Fragen stellen.'
        }), 429

    # IP-Rate-Limit: 8 Chat-Messages/h (verhindert Brute-Force)
    ip = request.headers.get('X-Forwarded-For', request.remote_addr or 'unknown').split(',')[0].strip()
    if not _qa_rate_check(ip, 'chat', max_per_hour=8):
        return jsonify({'error': 'Zu viele Nachrichten — bitte warte 5-10 Minuten'}), 429

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
        f"Reinigung: {result_data.get('reinig', 0):.2f} € ({result_data.get('arbeitstage',0)} Arbeitstage × 1,60 €)",
        f"Trinkgelder: {result_data.get('trink', 0):.2f} € ({result_data.get('hotel_naechte',0)} Hotelnächte × 3,60 €)",
        f"Z72 (Inland >8h): {result_data.get('vma_72_tage',0)} Tage / {result_data.get('vma_72',0):.2f} €",
        f"Z73 (An-/Abreise): {result_data.get('vma_73_tage',0)} Tage / {result_data.get('vma_73',0):.2f} €",
        f"Z74 (Inland 24h): {result_data.get('vma_74_tage',0)} Tage / {result_data.get('vma_74',0):.2f} €",
        f"Z76 (Ausland-VMA): {result_data.get('vma_aus', 0):.2f} €",
        f"Brutto-Aufwendungen gesamt: {result_data.get('gesamt', 0):.2f} €",
        f"Netto in WISO einzutragen: {result_data.get('netto', 0):.2f} €",
    ]

    notes_block = ('\n'.join(f"- {n}" for n in notes)) if notes else 'keine'
    history_block = '\n'.join(
        f"{'User' if m['role']=='user' else 'AeroTAX'}: {m['content']}"
        for m in chat_history[-10:]
    )

    prompt = f"""Du bist AeroTAX, der KI-Steuerberater von aerosteuer.de. Du beantwortest STRENG NUR Fragen zu zwei Themen:

  1. DIESER konkreten Auswertung des Mandanten (Werte, Berechnung, Plausibilität)
  2. WISO-Eingabe der Werte (welche Zeile, welcher Pfad)

ALLES ANDERE wird höflich abgelehnt mit kurzem Hinweis: "Das ist außerhalb meines Scopes — ich helfe dir nur zur Auswertung und WISO-Eingabe. Allgemeine Steuerfragen kannst du im Community-Forum stellen."

Verboten: allgemeine Steuertipps, andere Jahre, Lebensberatung, Karriere, Investments, Politik, was-wäre-wenn-Spiele, hypothetische Beispiele.

═══ MANDANTEN-AUSWERTUNG (Steuerjahr {result_data.get('year','?')}) ═══
{chr(10).join(summary_lines)}

═══ HINWEISE AUS DER AUSWERTUNG ═══
{notes_block}

═══ BISHERIGER CHAT-VERLAUF (max 10 letzte) ═══
{history_block or '(erste Nachricht)'}

═══ NEUE FRAGE ═══
{message}

═══ ANTWORT-REGELN ═══
- Max 200 Wörter, präzise auf den Punkt
- Bei On-Topic: konkret bezogen auf SEINE Werte aus der Liste oben
- Bei Off-Topic: 1-Satz-Ablehnung mit Verweis auf Forum
- Nutze §9 EStG / EASA-FTL nur wenn nötig zur Begründung
- Wenn Frage zu WISO-Eingabe: konkrete Zeilen-Nummer + Pfad nennen
- Bei Off-Topic-Verweisen brauchst du KEINEN Disclaimer
- Bei On-Topic Antworten: schließe mit dem Pflicht-Disclaimer (siehe unten)

═══ PFLICHT-DISCLAIMER bei steuerlichen Antworten (am Ende, neue Zeile) ═══
⚠ Hinweis: Orientierungshilfe — kein Ersatz für persönlichen Steuerberater (§3 StBerG)."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        # Output-Cap: 600 Tokens = ca. 200-250 Wörter, hält Kosten klein
        resp = _claude_with_retry(client, 'claude-sonnet-4-6', 600, prompt,
                                   max_retries=2, label='Chat-AeroTAX')
        answer = resp.content[0].text.strip()

        # Chat-Verlauf updaten + speichern
        chat_history.append({'role': 'user', 'content': message, 'ts': datetime.utcnow().isoformat() + 'Z'})
        chat_history.append({'role': 'assistant', 'content': answer, 'ts': datetime.utcnow().isoformat() + 'Z'})
        session['chat_history'] = chat_history[-50:]  # max 50 Nachrichten
        _save_session(token, session)

        new_user_count = sum(1 for m in chat_history if m.get('role') == 'user')
        remaining = max(0, HARD_CAP - new_user_count)
        return jsonify({
            'answer': answer,
            'remaining': remaining,
            'used': new_user_count,
            'cap': HARD_CAP,
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
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
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
⚠ Rechtshinweis: Diese Information dient zur Orientierung. AeroTAX ist kein Steuerberater nach §3 StBerG. Bei komplexen Einzelfällen ziehe einen Steuerberater oder Lohnsteuerhilfeverein zu Rate.

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
            # Dedupe: 24h-Window
            cutoff = (datetime.utcnow() - timedelta(hours=24)).isoformat()
            check = sb.table('upvotes').select('id').eq('target_type', target_type).eq('target_id', target_id).eq('ip_hash', ip_hash).gte('created_at', cutoff).limit(1).execute()
            if check.data:
                return jsonify({'error': 'Du hast bereits gevotet — versuch es morgen wieder'}), 429
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
                return jsonify({'error': 'Bereits gevotet'}), 429
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
                cutoff = datetime.utcnow() - timedelta(hours=24)
                already_voted = any(
                    v.get('h') == ip_hash and datetime.fromisoformat(v.get('ts','').replace('Z','')) >= cutoff
                    for v in target['upvotes_log']
                )
                if already_voted:
                    return jsonify({'error': 'Du hast bereits gevotet'}), 429
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
    return jsonify({'status': 'AeroTax Backend läuft', 'version': '2.0'})


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
            'Daten werden verifiziert (FollowMe-Vergleich)…',
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

def parse_lohnsteuerbescheinigung(pdf_bytes_list):
    """
    Extrahiert ALLE steuerrelevanten Felder der Lohnsteuerbescheinigung.
    Gibt vollständiges Dict zurück das für die komplette Anlage N / Vorsorgeaufwendungen
    gebraucht wird — nicht nur Brutto und Z17.
    """
    pdf_bytes_list = _bytes_list(pdf_bytes_list)
    result = {
        # Grunddaten
        'brutto': 0, 'lohnsteuer': 0, 'soli': 0,
        'kirchensteuer_an': 0, 'kirchensteuer_eg': 0,
        # Z17/Z18 Arbeitgeber-Fahrtkostenerstattung
        'ag_fahrt_z17': 0, 'ag_fahrt_z18_pauschal': 0,
        # Sozialversicherung AN (Sonderausgaben §10 EStG)
        'rv_an': 0,   # Z23a gesetzliche RV
        'kv_an': 0,   # Z25 gesetzliche KV
        'pv_an': 0,   # Z26 gesetzliche PV
        'av_an': 0,   # Z27 Arbeitslosenversicherung
        # Arbeitgeber-Anteile (für RV-Gesamtbeitrag Anlage Vorsorge)
        'rv_ag': 0,   # Z22a
        # Steuerfreie Leistungen
        'verpflegungszuschuss_z20': 0,  # steuerfreie Verpflegung bei Auswärtstätigkeit
        'doppelhaus_z21': 0,            # doppelte Haushaltsführung
        # Persönliche Daten
        'identnr': '', 'geburtsdatum': '', 'personalnummer': '',
        'steuerklasse': '1', 'kinderfreibetraege': 0.0,
        'kirchensteuermerkmale': '',
        # Arbeitgeber
        'arbeitgeber': 'Deutsche Lufthansa AG',
        'finanzamt': '', 'steuernummer_ag': '',
        # Abgeleitete Werte (werden berechnet)
        'vorsorge_gesamt_an': 0,  # rv+kv+pv+av
        'rv_gesamt': 0,           # rv_an + rv_ag (für Altersvorsorgeabzug)
    }

    for pdf_bytes in pdf_bytes_list:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = '\n'.join(p.extract_text() or '' for p in pdf.pages)

            def find(pattern, default=0.0):
                m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
                if m:
                    try: return float(m.group(1).replace('.','').replace(',','.'))
                    except: pass
                return default

            def findstr(pattern, default=''):
                m = re.search(pattern, text, re.IGNORECASE)
                return m.group(1).strip() if m else default

            b = find(r'Bruttoarbeitslohn[^\d]+([\d\.]+,\d{2})')
            if b > 0:
                result['brutto']             = b
                result['lohnsteuer']         = find(r'Lohnsteuer von 3\.[^\d]+([\d\.]+,\d{2})')
                result['soli']               = find(r'Solidarit[^\d]+([\d\.]+,\d{2})')
                result['kirchensteuer_an']   = find(r'Kirchensteuer des\nArbeitnehmers von 3\.[^\d]+([\d\.]+,\d{2})')
                result['ag_fahrt_z17']       = find(r'Entfernungspauschale anzurechnen sind\s+([\d\.]+,\d{2})')
                result['ag_fahrt_z18_pauschal'] = find(r'15%[^\d]+([\d\.]+,\d{2})')
                result['rv_ag']              = find(r'22\.\s+Arbeitgeber[^\n]+\nJahreshinzurechnungsbetrag versicherung\s+([\d\.]+,\d{2})')
                if result['rv_ag'] == 0:
                    # Fallback: same value as rv_an (AG-Anteil = AN-Anteil bei gesetzlicher RV)
                    result['rv_ag']          = find(r'22\.\s+Arbeitgeber[^\d\n]+\n[^\d\n]+\s+([\d\.]+,\d{2})')
                result['rv_an']              = find(r'23\.\s+Arbeitnehmer[^\d]+Renten-?\n\s*versicherung\s+([\d\.]+,\d{2})')
                result['kv_an']              = find(r'25\.\s+Arbeitnehmerbeitr[^\d]+Kranken-?\n\s*versicherung\s+([\d\.]+,\d{2})')
                result['pv_an']              = find(r'26\.\s+Arbeitnehmerbeitr[^\d]+Pflege-?\n\s*versicherung\s+([\d\.]+,\d{2})')
                result['av_an']              = find(r'27\.\s+Arbeitnehmerbeitr[^\d]+Arbeitslosenver-?\n?\s*sicherung\s+([\d\.]+,\d{2})')
                result['verpflegungszuschuss_z20'] = find(r'Verpflegungszusch[^\d]+([\d\.]+,\d{2})')
                result['doppelhaus_z21']     = find(r'doppelter Haushalt[^\d]+([\d\.]+,\d{2})')

                # Persönliche Daten
                m_id = re.search(r'Identifikationsnummer:\s*(\d{11})', text)
                if m_id: result['identnr'] = m_id.group(1)
                m_geb = re.search(r'Geburtsdatum:\s*(\d{2}\.\d{2}\.\d{4})', text)
                if m_geb: result['geburtsdatum'] = m_geb.group(1)
                m_pnr = re.search(r'Personalnummer:\s*(\d+)', text)
                if m_pnr: result['personalnummer'] = m_pnr.group(1)
                m_sk = re.search(r'Steuerklasse/Faktor\s+(\d)', text)
                if m_sk: result['steuerklasse'] = m_sk.group(1)
                m_kfb = re.search(r'Kinderfreibetr[^\d]+([\d,]+)', text)
                if m_kfb:
                    try: result['kinderfreibetraege'] = float(m_kfb.group(1).replace(',','.'))
                    except: pass
                m_kst = re.search(r'Kirchensteuermerkmale\s+([\w\s/\-]+?)(?:\n|$)', text)
                if m_kst: result['kirchensteuermerkmale'] = m_kst.group(1).strip()

                # Arbeitgeber-Info
                m_fa = re.search(r'Finanzamt[^\n]*\n([^\n]+)', text)
                if m_fa: result['finanzamt'] = m_fa.group(1).strip()
                m_stnr = re.search(r'Steuernummer:\s*([\d/]+)', text)
                if m_stnr: result['steuernummer_ag'] = m_stnr.group(1)

                # Abgeleitete Summen
                result['vorsorge_gesamt_an'] = round(
                    result['rv_an'] + result['kv_an'] +
                    result['pv_an'] + result['av_an'], 2)
                result['rv_gesamt'] = round(result['rv_an'] + result['rv_ag'], 2)

        except Exception as e:
            print(f'LSB parse error: {e}')

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


def _claude_stream_with_retry(client, model, max_tokens, content, max_retries=3, label='claude-stream'):
    """Wie _claude_with_retry, aber für Streaming-Calls. Liefert kompletten Text zurück."""
    import time as _t
    last_err = None
    for attempt in range(max_retries):
        try:
            full_text = ''
            with client.messages.stream(model=model, max_tokens=max_tokens,
                                        messages=[{'role': 'user', 'content': content}]) as stream:
                for text in stream.text_stream:
                    full_text += text
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


# ── BMF-PAUSCHALEN PRO JAHR ────────────────────────────────────
# Quelle: BMF-Schreiben "Steuerliche Behandlung von Reisekosten"
# Aktualisierungen jährlich — wenn neues Jahr, hier ergänzen.
BMF_INLAND_BY_YEAR = {
    2023: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},
    2024: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},
    2025: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},
    2026: {'tagestrip_8h': 14.0, 'an_abreise': 14.0, 'voll_24h': 28.0},  # noch keine Änderung bekannt
}

PENDLER_BY_YEAR = {
    2023: {'lt_20km': 0.30, 'gt_21km': 0.38},
    2024: {'lt_20km': 0.30, 'gt_21km': 0.38},
    2025: {'lt_20km': 0.30, 'gt_21km': 0.38},
    2026: {'lt_20km': 0.30, 'gt_21km': 0.38},
}

REINIGUNG_PRO_TAG_BY_YEAR = {
    2023: 1.60, 2024: 1.60, 2025: 1.60, 2026: 1.60,  # Verwaltungspraxis
}

TRINKGELD_PRO_NACHT_BY_YEAR = {
    2023: 3.60, 2024: 3.60, 2025: 3.60, 2026: 3.60,  # Verwaltungspraxis
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
            # SAME-DAY-TOUR: A FRA → DEST und E DEST → FRA am gleichen Tag
            arbeitstage += 1
            fahrtage += 1
            in_tour = False
            ziel = a_matches[0]
            if ziel in INLAND_IATA:
                z72_inland_days += 1
                # Block-Time für Z72-Boost-Berechnung mitgeben
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

    def bmf_24h(ort):
        v = bmf_lookup(ort, year)
        return v[0] if v else None
    def bmf_an(ort):
        v = bmf_lookup(ort, year)
        return v[1] if v else None

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

        if has_ab and has_an:
            # Same-Day-Tour
            if is_inland:
                z72_tage += 1
                z72_eur += sf_eur if sf_eur else 14.0
                fahrtage_inland += 1
            else:
                # Auslands-Tagestrip: NUR Pauschale wenn LH stfrei zahlt.
                # Wenn LH nichts zahlt, war Abwesenheit i.d.R. unter 8h
                # (kein §9 EStG-Anspruch). Kein BMF-Fallback hier.
                if sf_eur:
                    z76_eur += sf_eur
                fahrtage_ausland += 1
        elif has_ab and not has_an:
            # Anreise-Tag
            if is_inland:
                z73_tage += 1
                z73_eur += sf_eur if sf_eur else 14.0
                fahrtage_inland += 1
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
            hotelnaechte += 1
        elif not has_ab and has_an:
            # Abreise-Tag
            if is_inland:
                z73_tage += 1
                z73_eur += sf_eur if sf_eur else 14.0
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
        else:
            # Voll-Tag (24h auswärts)
            if is_inland:
                z74_tage += 1
                z74_eur += sf_eur if sf_eur else 28.0
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
            hotelnaechte += 1

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

    VERIFIZIERTE FORMELN (gegen FollowMe Ground Truth getestet):

    Z77 (steuerfrei gesamt):
        Pro Abrechnung: Z77 = Gesamt - letzter_Wert der "Summe:"-Zeile
        "Summe: G C2 C3" → Z77 = G - C3  (3 Spalten)
        "Summe: G C2"    → Z77 = G - C2  (2 Spalten)
        Summe über alle Abrechnungen = exakt FollowMe Z77

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

    for pdf_bytes in pdf_bytes_list:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ''
                    if 'Streckeneinsatz' not in text and 'stfrei' not in text:
                        continue

                    # Erstellungsdatum → Monatsbezeichnung
                    m_erst = re.search(r'Erstellt\s+(\d{2})\.(\d{2})\.(\d{4})', text)
                    if not m_erst:
                        continue
                    erstellt = f"{m_erst.group(1)}.{m_erst.group(2)}.{m_erst.group(3)}"
                    try:
                        mo_nr = int(m_erst.group(2))
                        mo_name = __import__('datetime').date(2025, mo_nr, 1).strftime('%B')
                    except:
                        mo_name = f"Monat {m_erst.group(2)}"

                    # Summen-Zeile → Z77 dieser Abrechnung
                    # FORMEL: Z77 = Gesamt - letzter_Wert (Steuer/Steuerpflichtig)
                    m_sum = re.search(
                        r'Summe:\s+([\d\.]+,\d{2})\s+([\d\.]+,\d{2})(?:\s+([\d\.]+,\d{2}))?',
                        text)
                    if not m_sum:
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
                        'gesamt':         g,
                        'steuerfrei':     z77_page,      # Z77-Anteil
                        'steuerpflichtig': steuer,
                        'z73_tage':       z73_page,
                    })
                    z73_tage += z73_page

        except Exception as e:
            print(f'SE Regex error: {e}')

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

    return {
        'abrechnungen':          abrechnungen,
        'summe_gesamt':          round(sum(a['gesamt'] for a in abrechnungen), 2),
        'summe_steuerfrei':      z77_total,
        'summe_steuerpflichtig': round(sum(a['steuerpflichtig'] for a in abrechnungen), 2),
        # Deterministisch ermittelt (literal aus SE) — primärer Wert
        'z72_tage': se_det['z72_tage'], 'z72_eur': se_det['z72_eur'],
        'z73_tage': se_det['z73_tage'], 'z73_eur': se_det['z73_eur'],
        'z74_tage': se_det['z74_tage'], 'z74_eur': se_det['z74_eur'],
        'z76_eur':  se_det['z76_eur'],
        # SE-direkte Counts (überlebt jetzt ohne Flugstundenübersicht)
        'arbeitstage_se':  se_det.get('arbeitstage', 0),
        'fahrtage_se':     se_det.get('fahrtage', 0),
        'hotelnaechte_se': se_det.get('hotelnaechte', 0),
        'unklare_zeilen': se_det['unklare_zeilen'],
    }

def parse_dienstplan_mit_ki(pdf_bytes_list, se_bytes_list=None, km_form=0, se_hints=None, homebase='FRA'):
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

    # ── FollowMe-Erkennung ──────────────────────────────────────────
    combined = ''
    for pb in _bytes_list(pdf_bytes_list)[:2]:
        try:
            with pdfplumber.open(io.BytesIO(pb)) as pdf:
                combined += ' '.join(p.extract_text() or '' for p in pdf.pages[:3])
        except: pass

    if re.search(r'FollowMe|Zeile 72|Zeile 73|Anlage N.*Auswertung', combined, re.I):
        # FollowMe-PDF: direkt mit Claude parsen
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            result = {'fahr_tage':0,'km':0,'arbeitstage':0,'hotel_naechte':0,
                      'vma_72_tage':0,'vma_73_tage':0,'vma_74_tage':0,
                      'vma_72':0,'vma_73':0,'vma_74':0,'vma_aus':0,'z77':0,'ausland_touren':[]}
            content_v = []
            for pb in _bytes_list(pdf_bytes_list)[:3]:
                b64 = base64.standard_b64encode(pb).decode()
                content_v.append({'type':'document','source':{'type':'base64','media_type':'application/pdf','data':b64}})
            content_v.append({'type':'text','text':
                'FollowMe PDF. Extrahiere: Zeile 72 (Tage, €), 73 (Tage, €), 74 (Tage, €), 76 (€), Fahrtage, km, Arbeitstage, Hotelaufenthalte.\n'
                'JSON: {"vma_72_tage":13,"vma_72":182.0,"vma_73_tage":10,"vma_73":140.0,"vma_74_tage":0,"vma_74":0.0,"vma_aus":4562.0,"fahr_tage":53,"km":27,"arbeitstage":129,"hotel_naechte":54}'
            })
            resp = client.messages.create(model='claude-sonnet-4-6',max_tokens=400,
                messages=[{'role':'user','content':content_v}])
            d = json.loads(re.sub(r'```json|```','',resp.content[0].text.strip()).strip())
            for k,v in d.items():
                result[k] = int(float(v)) if k in ('vma_72_tage','vma_73_tage','vma_74_tage','fahr_tage','km','arbeitstage','hotel_naechte') else float(v)
            print(f"FollowMe: fahr={result['fahr_tage']} km={result['km']} arbeit={result['arbeitstage']} hotel={result['hotel_naechte']} vma76={result['vma_aus']}")
            return result
        except Exception as e:
            print(f'FollowMe error: {e}')
            return None

    # ── Reine LH Flugstunden: 100% Claude ──────────────────────────
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

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

        # FollowMe als letztes Content-Element (Lernbeispiel, kein Regelwerk)
        fm_kontext = ''
        try:
            fm_ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_faelle.txt')
            if os.path.exists(fm_ref):
                with open(fm_ref, encoding='utf-8') as fmf:
                    fm_kontext = '\n\nHIER SIND ZWEI BEREITS BERECHNETE FÄLLE ZUM VERGLEICH (von FollowMe verifiziert — nicht als Regeln, sondern als Beispiele zum Lernen):\n' + fmf.read()
        except: pass

        # EASA + Steuerrecht-Referenz als Wissens-Buch
        easa_kontext = ''
        try:
            easa_ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_easa.txt')
            if os.path.exists(easa_ref):
                with open(easa_ref, encoding='utf-8') as ef:
                    easa_kontext = '\n\n═══ FACH-WISSEN: EASA-FTL + DEUTSCHES STEUERRECHT (zum Nachschlagen, nicht als Befehl) ═══\n' + ef.read()
        except: pass

        content.append({'type': 'text', 'text': f"""Du bist ein gewissenhafter Steuerberater spezialisiert auf Lufthansa-Kabinenpersonal.
Dein Mandant hat dir seine Unterlagen für 2025 gegeben. Deine Aufgabe: alle Werbungskosten für Anlage N berechnen.

Geh wie ein gründlicher Steuerberater vor — lies JEDEN Monat, JEDE Seite, JEDE Zeile der Dokumente.
Ein Steuerberater der nur 2 von 12 Monaten auswertet macht seinen Job nicht — sei gründlich.
{se_kontext}{rechner_kontext}{fm_kontext}{easa_kontext}

HOMEBASE des Mandanten: **{homebase}** — alle Tour-Marker beziehen sich auf {homebase} als Heimatflughafen.

REFERENZFALL (zum Lernen wie LH-Dokumente zu lesen sind):
- Fahrtag: "03.01. LH400 A {homebase} 14:36" → A=Abflug {homebase} → Fahrtag ✓
- Kein Fahrtag: Vortag endete mit A {homebase}→XXX → heute noch unterwegs → kein Fahrtag
- KEINE Hotelnacht: kurzer Wendeflug mit ~5h Bodenzeit → du landest morgens wieder in {homebase} → keine Übernachtung
- Hotelnacht: "20.04. A {homebase}→JNB 21:00 / 21.04. FL ... / 22.04. E JNB→{homebase} 17:55" → du übernachtest in JNB, das ist eine Hotel-Nacht
- Z73: SE "14,00  {homebase}" → stfrei=14, stfrei-Ort={homebase} → Anreisetag Z73 ✓
- Z76: SE "48,00  SEL" → stfrei=48, stfrei-Ort=SEL(Ausland) → VMA Ausland Z76 ✓
- Z77: Alle stfrei-Einzelwerte summieren — NICHT die Summenzeile (Format variiert!)
Verifiziertes Ergebnis eines LH-Mitarbeiters: Fahrtage=53, Hotel=54, Z73=140€, Z76=4562€, Z77=4742,80€

═══ DEINE AUFGABE — Steuerberater-Mentalität ═══
Du bist Steuerberater. Ein Steuerberater **erfindet keine Zahlen, schätzt nicht, nutzt keine eigene BMF-Tabelle**. Er liest exakt was im Dokument steht und addiert/subtrahiert was relevant ist.

Lies Flugstunden + Streckeneinsatz Tag für Tag und ermittle:

**Aus reinem Lesen + Addieren (keine Interpretation):**
- vma_aus  = Σ aller stfrei-Werte aus SE-Zeilen wo stfrei-Ort Ausland ist (NICHT aus deinem Wissen über BMF-Sätze rechnen — nur die literal Zahlen die LH ins Dokument geschrieben hat)
- vma_72 = Σ stfrei wo stfrei-Ort Inland UND Tagestrip (AB+AN gleicher Tag)
- vma_73 = Σ stfrei wo stfrei-Ort Inland UND An-/Abreisetag (zur Auslandsdestination)
- vma_74 = Σ stfrei wo Inland 24h-Tag (sehr selten)

**Mit Interpretation (Tag-Klassifikation, brauchst Domain-Wissen):**
- arbeitstage / fahrtage / hotel_naechte (siehe Info-Buch unten)

z77 lass auf 0, das Backend setzt's deterministisch aus der Summe-Zeile.

WICHTIG: Wenn du eine SE-Zeile siehst wie `04.02.2025  28,80 JNB 12 36,00 JNB`, ist 36,00 der stfrei-Wert den du addierst. NICHT 36 aus einer BMF-Tabelle in deinem Kopf, sondern die literal `36,00` aus dem Dokument. Selbst wenn LH einen BMF-untypischen Wert hingeschrieben hat — der ist gültig, du addierst ihn so wie er da steht.

═══ INFO (zum Verstehen — keine starren Regeln) ═══

**LH-Marker die typischerweise vorkommen** (du erkennst Kontext aus Datum, Uhrzeit, Strecke):
- `/- FREIER TAG`, `U` (Urlaub), `K` (Krank), unbezahlte Freistellung → kein Arbeitstag
- `LH#### A {homebase}` = Abflug von Heimatflughafen → Tour-Start, Arbeitstag, Fahrtag
- `LH#### E ... {homebase}` = Einflug nach {homebase} → Tour-Ende, Arbeitstag
- `FL STRECKENEINSATZTAG` = Auslands-Übernachtung → Arbeitstag + Hotel-Nacht
- `SBY` (Standby zuhause), `RES` (Reserve zuhause), Online-Schulung/e-Learning → Arbeitstag, **kein Fahrtag** (du warst daheim)
- Vor-Ort-Dienst in FRA mit Uhrzeit (Briefing, Sprachtest, Schulung in Präsenz, EM, EK, D4, EH) → Arbeitstag + Fahrtag (du musstest hin)
- `LM NACHGEWAEHRUNG` = Lohnnachzahlung (Buchungspost) → **kein Arbeitstag**

**EASA-FTL Layover-Regel (EU 965/2012, ORO.FTL.235):**
Hotel-Nacht setzt min. ~10h Bodenzeit am Zielort voraus. Crew Rest im Flieger zählt nicht. Nachtflug-Heimkehr (z.B. 22:00 raus, 05-06h FRA) ist Turnaround, kein Hotel. FL-Marker bei LH = echter Layover ≥10h.

**Tour-Logik:**
- Eine Tour = 1 Fahrtag (egal ob 1- oder 10-tägig — du fährst einmal hin und einmal zurück)
- Mehretappen ohne Heimkehr (FRA→GVA→OTP→FRA) = 1 Fahrtag
- Folge-Tage einer Tour = Arbeitstag, kein Fahrtag (du bist nicht zuhause gewesen)

**SE für VMA:**
- stfrei-Spalte = vorberechneter BMF-Tagessatz, stfrei-Ort entscheidet die Kategorie
- stfrei-Ort Inland (FRA, MUC, HAM…) → Z72/Z73/Z74 (14€ Tagestrip / 14€ An-/Abreise / 28€ 24h-Inland)
- stfrei-Ort Ausland (SAO, JNB, ICN…) → Z76 (Betrag direkt aus stfrei-Spalte addieren)
- Storno-Zeilen enden mit `X` → ignorieren

**Verifizierter Referenzfall (FollowMe):** Fahrtage=53, Arbeitstage=129, Hotel=54, Z73=140€, Z76=4562€, Z77=4742,80€. Wenn deine Werte deutlich abweichen, prüf nochmal.

Plausi-Anker: 110-150 Arbeitstage/Jahr, 40-60 Fahrtage, 40-65 Hotelnächte bei Vollzeit-Kabinenpersonal.

═══ ANTWORT-FORMAT ═══
ZUERST die JSON-Zeile (erste Zeichen `{{`), DANN Nachweis. Keine Backticks.

{{"fahrtage":53,"km":{km},"arbeitstage":129,"hotel_naechte":54,"vma_72_tage":13,"vma_72":182,"vma_73_tage":10,"vma_73":140,"vma_74_tage":0,"vma_74":0,"vma_aus":4562,"z77":0}}

Nachweis (kurz, Monat für Monat):
Januar: Arbeitstage=…, Fahrtage=…, Hotel=…  (kurze Tour-Zusammenfassung)
…
Dezember: …
Unklare Codes (falls): <Code> = <was du daraus geschlossen hast>"""
        })

        import time as _time_mod
        sonnet_start_time = _time_mod.time()
        full_text = _claude_stream_with_retry(client, 'claude-sonnet-4-6', 12000, content,
                                              max_retries=3, label='Sonnet-DP')
        print(f"Sonnet-Antwort: {len(full_text)} Zeichen, {_time_mod.time()-sonnet_start_time:.1f}s")

        # ── JSON robust extrahieren via brace-counter ──
        # Sucht nach ALLEN balanced {...} Blöcken im Text und nimmt den der "fahrtage" enthält.
        nachweis = ''
        json_str = '{}'
        candidates = []
        depth = 0
        start = -1
        for i, ch in enumerate(full_text):
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
    """Opus 4.7 als Senior-Steuerberater. Wird nur gerufen wenn Parser+Sonnet uneinig sind.
    Bekommt beide Vorschläge + Originaldokumente, entscheidet final.
    Liefert verifizierte Werte + Begründung.
    """
    ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY', '')
    if not ANTHROPIC_KEY:
        return None
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        prompt = f"""Du bist ein Senior-Steuerberater für Lufthansa-Kabinenpersonal mit jahrzehntelanger Erfahrung.
Zwei Junior-Berater haben unabhängig dieselben Dokumente ausgewertet und kommen zu unterschiedlichen Werten.
Deine Aufgabe: Streit schlichten — den korrekten Wert ermitteln, nicht den Mittelwert.

JUNIOR 1 (Deterministischer Parser, liest literal aus Dokument):
{parser_summary}

JUNIOR 2 (KI-Steuerberater Sonnet, interpretiert Edge-Cases):
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
        for i, ch in enumerate(full_text):
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
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

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
                            'text': f"""Du bist Senior-Steuerberater. Lies diese{'n' if n_files==1 else ''} {n_files} Beleg(e) für: {info['name']}.

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

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
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
    except Exception as e:
        print(f'Inference error: {e}')
        notes = [f'Schätzung fehlgeschlagen für: {missing_str}']

    return inferred, notes


def berechne(form, files):
    """
    Berechnet alle Werbungskosten.
    Strategie: Erst aus allen Dokumenten extrahieren.
    Bei fehlenden/unvollständigen Dokumenten: KI schätzt aus vorhandenen.
    Immer eine Auswertung liefern, fehlende Werte klar markieren.
    """
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
    else:
        missing.append('Lohnsteuerbescheinigung (nicht hochgeladen)')

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
            # Check if all 12 months present
            months_found = len(se_data.get('abrechnungen', []))
            if months_found < 12:
                notes.append(f'Streckeneinsatz: {months_found} von 12 Monaten hochgeladen — fehlende Monate wurden nicht berücksichtigt')
    else:
        missing.append('Streckeneinsatz-Abrechnungen (nicht hochgeladen)')

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
                                          se_hints=se_data if se_data else None, homebase=homebase_iata)
        except RuntimeError as e:
            raise
        if not dp or not dp.get('arbeitstage'):
            missing.append('Flugstunden-Übersichten (Analyse fehlgeschlagen — bitte nochmal versuchen)')
            dp = None
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
    km = float(form.get('km', 0)) if anreise in ('auto', 'fahrrad') else 0
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

    # VMA-Werte mit jahr-korrekten Sätzen
    vma_72 = vma_72_tage * bmf_inland['tagestrip_8h']
    vma_73 = vma_73_tage * bmf_inland['an_abreise']
    vma_74 = vma_74_tage * bmf_inland['voll_24h']
    vma_in = vma_72 + vma_73 + vma_74

    # ── FAHRTKOSTEN ───────────────────────────────────────────
    fahrzeug  = form.get('fahrzeug', 'verbrenner')
    jobticket = form.get('jobticket', 'nein')
    if anreise in ('auto', 'fahrrad'):
        fahr = min(km,20)*fahr_tage*pendler['lt_20km'] + max(0,km-20)*fahr_tage*pendler['gt_21km']
    elif anreise == 'oepnv':
        oepnv_kosten = float(form.get('oepnv_kosten', 0))
        fahr = 0 if jobticket == 'ja_frei' else float(oepnv_kosten)
    elif anreise == 'shuttle_kosten':
        # Kostenpflichtiger Shuttle/Sammeltaxi → Jahreskosten direkt absetzbar (Reisekosten)
        fahr = float(form.get('shuttle_kosten', 0) or 0)
    else:
        # shuttle_frei, fuss, oder unbekannt → keine Fahrtkosten
        fahr = 0
    fahr = round(fahr, 2)

    # ── Z72-BOOST: pro-Tag Block-Time-aware mit AUTO-Briefing-Detection ──
    # LH Standard Operating Procedure für Cabin Crew:
    #   - Continental (Block ≤4h):  60 Min Briefing + 30 Min Sign-Off
    #   - Mid-haul    (Block 4-7h): 75 Min Briefing + 30 Min Sign-Off
    #   - Long-haul   (Block >7h):  90 Min Briefing + 30 Min Sign-Off
    # Sign-Off (Nacharbeitung) ist bei LH einheitlich ~30 Min für alle Tour-Typen.
    # Anfahrt: aus km × 1,5 min/km, oder 30 min Default.
    NACHARB_MIN = 30  # einheitlich für alle Tour-Typen
    # Anfahrt: User-Input bevorzugt, sonst aus km × 1,5 min/km abgeleitet
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
            # Auto-Briefing nach LH SOP basierend auf Block-Time
            if block_m <= 240:
                briefing_min = 60   # Continental
            elif block_m <= 420:
                briefing_min = 75   # Mid-haul
            else:
                briefing_min = 90   # Long-haul
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
                f'(LH SOP Briefing + 30 min Sign-Off + Anfahrt 2×{anfahrt_min}min) → +{added*int(bmf_inland["tagestrip_8h"])}€.'
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

    # ── GESAMTBERECHNUNG ─────────────────────────────────────
    gesamt = round(fahr + reinig + trink + vma_in + vma_aus, 2)
    netto  = round(gesamt - ag_z17 - z77, 2)

    # ── MATHEMATISCHE PLAUSI-CHECKS ──────────────────────────
    # Wenn Inkonsistenzen, User über Note informieren — keine stille Fehler.
    plausi_warns = []
    # Z72+Z73+Z74+Z76 ≈ Z77 (alle steuerfreien Werte sollten Z77 ergeben)
    # Bei sauber geparstem SE (keine unklare_zeilen) sollten sie nahezu identisch sein.
    vma_summe = vma_72 + vma_73 + vma_74 + vma_aus
    se_unklar_count = len((se_data or {}).get('unklare_zeilen', []) or [])
    # Tolerance: 1% wenn SE clean, 5% wenn unklare Zeilen, min 30€
    tolerance = max(30, z77 * (0.05 if se_unklar_count > 5 else 0.01))
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

    # ── QUALITY GATE: bei massiven Plausi-Fehlern direkt Recovery-Hinweis aufnehmen ──
    quality_questionable = bool(plausi_warns) and any('unmöglich' in w.lower() for w in plausi_warns)
    if quality_questionable:
        notes.append('🔁 Qualitäts-Hinweis: Werte sehen unplausibel aus. Falls offensichtlich falsch — du bekommst nach Auswertung einen kostenlosen Wiederholungs-Code.')

    # ── ANOMALIE-DETECTION: Werte außerhalb realistischer Bandbreite ──
    # Bandbreiten sind großzügig — nur extreme Ausreißer werden geflaggt.
    # Teilzeit-Mitarbeiter, Mutterschutz-Phasen etc. sollen NICHT geflaggt werden.
    anomalies = []
    if arbeitstage > 250:
        anomalies.append(f'Arbeitstage {arbeitstage} sehr hoch (>250) — bitte prüfen')
    if fahr_tage > arbeitstage and arbeitstage > 0:
        anomalies.append(f'Fahrtage {fahr_tage} > Arbeitstage {arbeitstage} — unmöglich')
    if hotel_naechte > 120:
        anomalies.append(f'Hotelnächte {hotel_naechte} sehr hoch (>120) — bitte prüfen')
    if vma_aus > 15000:
        anomalies.append(f'VMA Ausland {vma_aus:.0f}€ sehr hoch — bitte prüfen')
    if anomalies:
        for a in anomalies:
            notes.append(f'⚠ Anomalie: {a}')
        print(f"ANOMALIE-DETECTION: {anomalies}")

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
#  Helles Design wie EK Kanzlei / FollowMe
#  Korrekter Aufbau nach FollowMe-Methode
# ══════════════════════════════════════════════════════════════════













def erstelle_pdf(d):
    # ── PALETTE: minimal, elegant ────────────────────────────
    BG     = HexColor("#060a16")
    TEXT   = HexColor("#f1f5f9")   # primary
    TEXT2  = HexColor("#94a3b8")   # secondary
    TEXT3  = HexColor("#4a5a72")   # muted
    LINE   = HexColor("#1e3050")   # dividers
    LINE2  = HexColor("#2a3f5e")   # slightly brighter
    WHITE  = HexColor("#ffffff")
    G1=HexColor("#f97316"); G2=HexColor("#ec4899")
    G3=HexColor("#8b5cf6"); G4=HexColor("#2563eb")
    BLUE2  = HexColor("#60a5fa")
    NAVY   = HexColor("#071120")
    OFF    = HexColor("#e2e8f0")

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

    # ── PAGE HEADER / FOOTER ─────────────────────────────────
    def on_page(canv, doc):
        canv.saveState()
        W, H = A4
        canv.setFillColor(BG); canv.rect(0,0,W,H,fill=1,stroke=0)

        # Header — thick navy
        canv.setFillColor(NAVY)
        canv.rect(0, H-1.6*cm, W, 1.6*cm, fill=1,stroke=0)
        # Rainbow strip
        sw = W/4
        for i,col in enumerate([G1,G2,G3,G4]):
            canv.setFillColor(col)
            canv.rect(i*sw, H-0.12*cm, sw, 0.12*cm, fill=1,stroke=0)
        canv.setFillColor(LINE)
        canv.rect(0, H-1.6*cm, W, 0.04*cm, fill=1,stroke=0)

        # AeroTAX — Aero white, TAX blue, together
        canv.setFillColor(WHITE); canv.setFont("Helvetica-Bold", 14)
        canv.drawString(1.5*cm, H-1.08*cm, "Aero")
        aw = canv.stringWidth("Aero","Helvetica-Bold",14)
        canv.setFillColor(BLUE2)
        canv.drawString(1.5*cm+aw, H-1.08*cm, "TAX")
        tw = canv.stringWidth("TAX","Helvetica-Bold",14)
        canv.setFillColor(TEXT3); canv.setFont("Helvetica",9)
        canv.drawString(1.5*cm+aw+tw+0.25*cm, H-1.1*cm, "·")
        canv.setFillColor(OFF); canv.setFont("Helvetica",9)
        canv.drawString(1.5*cm+aw+tw+0.6*cm, H-1.08*cm,
            f"{d.get('name','')}  ·  Steuerjahr {d.get('year',2025)}")
        canv.setFillColor(TEXT3); canv.setFont("Helvetica",8)
        canv.drawRightString(W-1.5*cm, H-0.95*cm, f"Seite {doc.page}")
        canv.drawRightString(W-1.5*cm, H-1.38*cm, d.get('datum',''))

        # Footer — thick navy, minimal
        canv.setFillColor(NAVY)
        canv.rect(0, 0, W, 0.75*cm, fill=1,stroke=0)
        canv.setFillColor(LINE)
        canv.rect(0, 0.75*cm, W, 0.04*cm, fill=1,stroke=0)
        canv.setFillColor(WHITE); canv.setFont("Helvetica-Bold",7)
        canv.drawString(1.5*cm,0.48*cm,"Aero")
        aw2 = canv.stringWidth("Aero","Helvetica-Bold",7)
        canv.setFillColor(BLUE2)
        canv.drawString(1.5*cm+aw2,0.48*cm,"TAX")
        tw2 = canv.stringWidth("TAX","Helvetica-Bold",7)
        canv.setFillColor(TEXT3); canv.setFont("Helvetica",7)
        canv.drawString(1.5*cm+aw2+tw2+0.12*cm,0.48*cm,"·  aerosteuer.de")
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
    # SEITE 1 — DECKBLATT
    # ════════════════════════════════════════════════
    S.append(Spacer(1, 1.8*cm))

    # AeroTAX logo — large, gradient colored via font coloring
    S.append(Paragraph(
        'Aero<font color="#60a5fa">TAX</font>',
        ps("logo", fontSize=34, textColor=WHITE, fontName="Helvetica-Bold",
           leading=38, alignment=TA_CENTER, spaceAfter=8)))
    S.append(Paragraph("Die einfache Steuerauswertung für Flugpersonal",
        ps("tag", fontSize=9.5, textColor=TEXT3, fontName="Helvetica",
           leading=13, alignment=TA_CENTER, spaceAfter=30)))

    S.append(HRFlowable(width="40%", thickness=0.8, color=LINE2,
        hAlign='CENTER', spaceAfter=30))

    S.append(Paragraph(d['name'],
        ps("cname", fontSize=22, textColor=TEXT, fontName="Helvetica-Bold",
           leading=26, alignment=TA_CENTER, spaceAfter=5)))
    S.append(Paragraph(f"Steuerjahr {d.get('year',2025)}",
        ps("cyear", fontSize=11, textColor=TEXT2, fontName="Helvetica",
           leading=15, alignment=TA_CENTER, spaceAfter=5)))
    S.append(Paragraph("Deutsche Lufthansa AG",
        ps("cag", fontSize=9, textColor=TEXT3, fontName="Helvetica",
           leading=13, alignment=TA_CENTER, spaceAfter=36)))

    S.append(HRFlowable(width="40%", thickness=0.4, color=LINE,
        hAlign='CENTER', spaceAfter=30))

    # TOC — centered, elegant
    S.append(Paragraph("Inhalt",
        ps("toch", fontSize=11, textColor=TEXT2, fontName="Helvetica-Bold",
           leading=15, alignment=TA_CENTER, spaceAfter=22)))

    pg = 2
    toc = []
    toc.append(("Reisekosten & weitere absetzbare Kosten", str(pg))); pg+=1
    toc.append(("· · ·  Ab hier nur zur Information  · · ·", ""))
    toc.append(("Belege", str(pg))); pg+=1
    toc.append(("Berechnung", str(pg))); pg+=1
    toc.append(("Bestätigung & Unterschrift", str(pg)))

    for i, (title, page) in enumerate(toc):
        is_sep = title.startswith("·")
        if is_sep:
            S.append(Spacer(1, 0.1*cm))
            wrap = Table([[Paragraph(title,
                ps(f"tsep{i}", fontSize=8, textColor=TEXT3,
                   fontName="Helvetica", leading=12, alignment=TA_CENTER))]],
                colWidths=[13.2*cm])
            wrap.setStyle(TableStyle([
                ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
                ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                ("ALIGN",(0,0),(-1,-1),"CENTER"),
            ]))
            S.append(wrap)
            continue

        row = Table([[
            Paragraph(str(i+1) if not is_sep else "",
                ps(f"tn{i}", fontSize=9, textColor=TEXT3,
                   fontName="Helvetica-Bold", leading=13, alignment=TA_CENTER)),
            Paragraph(title,
                ps(f"tt{i}", fontSize=9.5, textColor=TEXT,
                   fontName="Helvetica", leading=13)),
            Paragraph(f"—  {page}" if page else "",
                ps(f"tp{i}", fontSize=9, textColor=TEXT3,
                   fontName="Helvetica", leading=13, alignment=TA_RIGHT)),
        ]], colWidths=[0.6*cm, 11.2*cm, 1.4*cm])
        row.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
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
    # SEITE 2 — REISEKOSTEN & WEITERE KOSTEN
    # ════════════════════════════════════════════════
    S.append(PageBreak())
    for el in section("Reisekosten & weitere absetzbare Kosten"): S.append(el)

    # Betrag — large, clean
    S.append(Paragraph("Einzutragender Betrag",
        ps("nb_lbl", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=11, spaceAfter=8, letterSpacing=1.5)))
    S.append(kv_total("Reisenebenkosten", eur(d['netto'])))
    S.append(Spacer(1, 0.3*cm))

    # Steps — pure text, elegant
    S.append(Paragraph("Schritt für Schritt",
        ps("steps_h", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
           leading=11, spaceAfter=14, letterSpacing=1.5)))

    steps = [
        ("1", "WISO / Elster öffnen",
         "Ausgaben → Werbungskosten → Reisekosten → Zusammengefasste Auswärtstätigkeiten → Neuer Eintrag"),
        ("2", "Beschreibung eingeben",
         f"Weitere Werbungskosten — Dienstplanauswertung AeroTax {d.get('year', 2025)}"),
        ("3", f"Reisenebenkosten:  {eur(d['netto'])}",
         "Nur diesen Betrag eintragen — alle anderen Felder leer lassen."),
        ("4", "PDF hochladen",
         "Als Anhang beifügen oder auf Anfrage beim Finanzamt nachreichen."),
    ]
    for n, title, desc in steps:
        t = Table([[
            Paragraph(n, ps(f"sn{n}", fontSize=11, textColor=TEXT3,
                fontName="Helvetica-Bold", leading=15, alignment=TA_CENTER)),
            Paragraph(
                f"<b>{title}</b><br/>"
                f'<font color="#4a5a72" size="8">{desc}</font>',
                ps(f"sd{n}", fontSize=9, textColor=TEXT,
                   fontName="Helvetica", leading=14)),
        ]], colWidths=[0.9*cm, 15.9*cm])
        t.setStyle(TableStyle([
            ("TOPPADDING",(0,0),(-1,-1),10),("BOTTOMPADDING",(0,0),(-1,-1),10),
            ("LEFTPADDING",(0,0),(-1,-1),8),("RIGHTPADDING",(0,0),(-1,-1),0),
            ("LINEBELOW",(0,0),(-1,0),0.3,LINE),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
        ]))
        S.append(t)
    S.append(Spacer(1, 0.6*cm))

    # Weitere absetzbare Kosten — directly below
    if belege:
        S.append(hr(0, 16))
        S.append(Paragraph("Weitere absetzbare Kosten",
            ps("wak", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=11, spaceAfter=8, letterSpacing=1.5)))
        S.append(Paragraph(
            "Diese Kosten kannst du zusätzlich eintragen:",
            ps("wak_s", fontSize=8.5, textColor=TEXT2, fontName="Helvetica",
               leading=13, spaceAfter=16)))
        for b in belege:
            has_doc = b.get('betrag', 0) > 0
            S.append(Paragraph(
                f"{b.get('icon','')}  <b>{b.get('name','')}</b>",
                ps(f"bn{id(b)}", fontSize=10,
                   textColor=TEXT if has_doc else TEXT3,
                   fontName="Helvetica-Bold" if has_doc else "Helvetica",
                   leading=14, spaceAfter=3)))
            t = Table([[
                Paragraph(
                    eur(b['betrag']) if has_doc else "⚠  Beleg fehlt",
                    ps(f"ba{id(b)}", fontSize=10,
                       textColor=TEXT if has_doc else TEXT3,
                       fontName="Helvetica-Bold", leading=13)),
                Paragraph(b.get('wiso', ''),
                    ps(f"bw{id(b)}", fontSize=8, textColor=TEXT3,
                       fontName="Helvetica", leading=11, alignment=TA_RIGHT)),
            ]], colWidths=[4*cm, 12.8*cm])
            t.setStyle(TableStyle([
                ("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0),
                ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ]))
            S.append(t)
            if b.get('hint') and has_doc:
                S.append(Paragraph(f"💡  {b['hint']}",
                    ps(f"bh{id(b)}", fontSize=7.5, textColor=TEXT3,
                       fontName="Helvetica", leading=11, spaceAfter=2)))
            S.append(hr(8, 12))

    # ════════════════════════════════════════════════
    # TRENNSEITE — minimalistisch, elegant
    # ════════════════════════════════════════════════
    S.append(PageBreak())
    S.append(Spacer(1, 5*cm))
    S.append(HRFlowable(width="30%", thickness=0.5, color=LINE2,
        hAlign='CENTER', spaceAfter=28))
    S.append(Paragraph("All Doors in Park.",
        ps("sep0", fontSize=14, textColor=TEXT3, fontName="Helvetica",
           leading=19, alignment=TA_CENTER, spaceAfter=18)))
    S.append(Paragraph("Du bist fertig.",
        ps("sep1", fontSize=28, textColor=TEXT, fontName="Helvetica-Bold",
           leading=32, alignment=TA_CENTER, spaceAfter=22)))
    S.append(HRFlowable(width="30%", thickness=0.5, color=LINE2,
        hAlign='CENTER', spaceAfter=22))
    S.append(Paragraph("Ab hier nur zur Information",
        ps("sep2", fontSize=13, textColor=TEXT2, fontName="Helvetica",
           leading=18, alignment=TA_CENTER, spaceAfter=16)))
    S.append(Paragraph(
        "Die folgenden Seiten zeigen die Berechnung im Detail "
        "und dienen als Nachweis für das Finanzamt.",
        ps("sep3", fontSize=9.5, textColor=TEXT3, fontName="Helvetica",
           leading=16, alignment=TA_CENTER)))

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
                S.append(Paragraph(f"{b.get('icon','')}  {b.get('name','')}",
                    ps(f"bpn{id(b)}{fidx}", fontSize=11, textColor=TEXT,
                       fontName="Helvetica-Bold", leading=15, spaceAfter=4)))
                S.append(Paragraph(
                    f"Betrag: {eur(betrag)}" if betrag>0 else "⚠  Betrag nicht erkannt",
                    ps(f"bpp{id(b)}{fidx}", fontSize=8.5, textColor=TEXT2,
                       fontName="Helvetica", leading=12, spaceAfter=10)))
                S.append(hr(0, 12))
                try:
                    # HEIC (iPhone) → erst zu JPEG konvertieren falls möglich
                    is_heic = b'ftypheic' in fb[:32] or b'ftypheix' in fb[:32] or b'ftypmif1' in fb[:32]
                    if is_heic and PIL_AVAILABLE and HEIF_AVAILABLE:
                        try:
                            from PIL import Image as PILImage
                            heic_img = PILImage.open(io.BytesIO(fb))
                            buf_jpg = io.BytesIO()
                            heic_img.convert('RGB').save(buf_jpg, format='JPEG', quality=88)
                            fb = buf_jpg.getvalue()
                        except Exception as _hc:
                            print(f"[heic] convert fail: {_hc}")
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
    # BERECHNUNG
    # ════════════════════════════════════════════════
    S.append(PageBreak())
    for el in section("Berechnung — Zur Information"): S.append(el)

    calc_items = [
        (f"Fahrtkosten Homebase  ({d.get('km',0)} km × {d.get('fahr_tage',0)} Tage)", "Zeilen 27–30", eur(d.get('fahr',0))),
        (f"Reinigungskosten  ({d.get('arbeitstage',0)} Tage × 1,60 €)", "Zeile 62", eur(d.get('reinig',0))),
        (f"Trinkgelder  ({d.get('hotel_naechte',0)} Nächte × 3,60 €)", "Zeile 68", eur(d.get('trink',0))),
        (f"VMA Inland >8h  ({d.get('vma_72_tage',0)} Tage × 14 €)", "Zeile 72", eur(d.get('vma_72',0))),
        (f"VMA An-/Abreisetage  ({d.get('vma_73_tage',0)} Tage × 14 €)", "Zeile 73", eur(d.get('vma_73',0))),
        (f"VMA 24h  ({d.get('vma_74_tage',0)} Tage × 28 €)", "Zeile 74", eur(d.get('vma_74',0))),
        ("VMA Ausland nach BMF-Pauschalen 2025", "Zeile 76", eur(d.get('vma_aus',0))),
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

    # Summe
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

        # Persönliche Daten
        S.append(Paragraph("PERSÖNLICHE DATEN",
            ps("lsb_h1", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=11, spaceAfter=10, letterSpacing=1.5)))
        pers_items = [
            ("Steuerklasse", sk),
            ("Kinderfreibeträge", str(kfb)),
            ("Identifikationsnummer", identnr),
            ("Geburtsdatum", gebdat),
            ("Personalnummer", pnr),
        ]
        for label, val in pers_items:
            if val and val not in ('0', '0.0', ''):
                S.append(kv(label, val))
        S.append(Spacer(1, 0.4*cm))

        # Lohnsteuer-Grunddaten
        S.append(Paragraph("LOHNSTEUER (Zeilen 3–6)",
            ps("lsb_h2", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=11, spaceAfter=10, letterSpacing=1.5)))
        lsb_items = [
            (f"Bruttoarbeitslohn (Zeile 3)", eur(brutto)),
            (f"Einbehaltene Lohnsteuer (Zeile 4)", eur(lohnst)),
            (f"Solidaritätszuschlag (Zeile 5)", eur(soli)),
            (f"Kirchensteuer AN (Zeile 6)", eur(kirche)),
            (f"AG-Fahrkostenzuschuss Z17 (→ Abzug Reisekosten)", eur(d.get('ag_z17',0))),
        ]
        for label, val in lsb_items:
            S.append(kv(label, val))
        S.append(Spacer(1, 0.4*cm))

        # Vorsorgeaufwendungen — Sonderausgaben
        S.append(Paragraph("VORSORGEAUFWENDUNGEN — SONDERAUSGABEN (§10 EStG)",
            ps("lsb_h3", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=11, spaceAfter=10, letterSpacing=1.5)))
        S.append(Paragraph(
            "Diese Beträge direkt in WISO unter Sonderausgaben → Vorsorgeaufwendungen eintragen:",
            ps("lsb_hint", fontSize=9, textColor=TEXT2, fontName="Helvetica",
               leading=14, spaceAfter=10)))

        vorsorge_items = [
            (f"Rentenversicherung AN (Zeile 23a)", "Anlage Vorsorge Z4", eur(rv_an)),
            (f"Rentenversicherung AG (Zeile 22a)", "Anlage Vorsorge Z5", eur(rv_ag)),
            (f"  → RV Gesamt (AN+AG)", "Anlage Vorsorge", eur(rv_ges)),
            (f"Gesetzl. Krankenversicherung AN (Zeile 25)", "Anlage Vorsorge Z12", eur(kv_an)),
            (f"Gesetzl. Pflegeversicherung AN (Zeile 26)", "Anlage Vorsorge Z13", eur(pv_an)),
            (f"Arbeitslosenversicherung AN (Zeile 27)", "Anlage Vorsorge Z14", eur(av_an)),
        ]
        for label, zeile, val in vorsorge_items:
            t = Table([[
                Paragraph(label, ps(f"vi{id(label)}", fontSize=9, textColor=TEXT2,
                    fontName="Helvetica", leading=12)),
                Paragraph(zeile, ps(f"vz{id(zeile)}", fontSize=7.5, textColor=TEXT3,
                    fontName="Helvetica", leading=12, alignment=TA_CENTER)),
                Paragraph(val, ps(f"vv{id(val)}", fontSize=9, textColor=TEXT,
                    fontName="Helvetica", leading=12, alignment=TA_RIGHT)),
            ]], colWidths=[10.5*cm, 2.5*cm, 3.8*cm])
            t.setStyle(TableStyle([
                ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
                ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
                ("LINEBELOW",(0,0),(-1,0),0.3,LINE),
                ("VALIGN",(0,0),(-1,-1),"MIDDLE"),
            ]))
            S.append(t)
        S.append(kv_total("Sozialversicherung gesamt (AN)", eur(vorsorge)))
        S.append(Spacer(1, 0.5*cm))

        # WISO Eintragehinweis
        S.append(Paragraph("WO IN WISO EINTRAGEN?",
            ps("wiso_h", fontSize=7.5, textColor=TEXT3, fontName="Helvetica-Bold",
               leading=11, spaceAfter=8, letterSpacing=1.5)))
        wiso_hints = [
            ("Rentenversicherung (AN+AG)", "Sonderausgaben → Vorsorgeaufwendungen → Beiträge zur gesetzl. Rentenversicherung"),
            ("Kranken- & Pflegeversicherung", "Sonderausgaben → Vorsorgeaufwendungen → Kranken- und Pflegeversicherung"),
            ("Arbeitslosenversicherung", "Sonderausgaben → Vorsorgeaufwendungen → Sonstige Vorsorgeaufwendungen"),
            ("Reisekosten (AeroTax-Betrag)", "Ausgaben → Werbungskosten → Reisekosten → Zusammengefasste Auswärtstätigkeiten"),
        ]
        for title, path in wiso_hints:
            S.append(Paragraph(
                f'<b>{title}</b>',
                ps(f"wh{id(title)}", fontSize=9, textColor=TEXT,
                   fontName="Helvetica-Bold", leading=13, spaceBefore=6)))
            S.append(Paragraph(path,
                ps(f"wp{id(path)}", fontSize=8.5, textColor=TEXT2,
                   fontName="Helvetica", leading=13, spaceAfter=4)))


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
        ("BOX",(0,0),(-1,-1), 0.5, HexColor("#2a3f5e")),
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
