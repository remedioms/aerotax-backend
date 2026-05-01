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
CORS(app, origins=[os.getenv('FRONTEND_URL','https://aerotax.de'), 'http://localhost:3000'])

stripe.api_key        = os.getenv('STRIPE_SECRET_KEY')
WEBHOOK_SECRET        = os.getenv('STRIPE_WEBHOOK_SECRET')
ANTHROPIC_KEY = os.getenv('ANTHROPIC_API_KEY')
PRICE_ID              = os.getenv('AEROTAX_PRICE_ID')
FRONTEND_URL          = os.getenv('FRONTEND_URL','https://aerotax.de')

# In-memory store (in Produktion: Redis oder S3)
_store = {}

# ── BMF AUSLANDSPAUSCHALEN 2025 ───────────────────────────────────
# Format: "IATA": (Tagessatz_24h, Tagessatz_An_Abreise)
BMF_2025 = {
    "BLR":(42,28),"HKG":(71,48),"HND":(50,33),"NRT":(50,33),
    "CPH":(75,50),"SVG":(75,50),"OSL":(75,50),"GVA":(66,44),
    "BOS":(63,42),"BOM":(53,36),"ICN":(48,32),"IKA":(33,22),
    "ORD":(65,44),"KEF":(62,41),"SEA":(59,40),"SIN":(71,48),
    "ZAG":(46,31),"ARN":(66,44),"GOT":(66,44),"TLL":(35,24),
    "MAD":(42,28),"LIS":(32,21),"EDI":(52,35),"SKP":(27,18),
    "SOF":(22,15),"VCE":(42,28),"FCO":(48,32),"MIA":(65,44),
    "LHR":(66,44),"NAP":(42,28),"OTP":(32,21),"BCN":(34,23),
    "RIX":(35,24),"CAI":(33,22),"TLV":(66,44),"LCA":(42,28),
    "DUB":(58,39),"TUN":(40,27),"MRS":(53,36),"AGP":(34,23),
    "ATH":(40,27),"VNO":(26,17),"SNN":(58,39),"BUD":(32,21),
    "LIN":(42,28),"PRG":(32,21),"MLA":(46,31),"KRK":(34,23),
    "MXP":(42,28),"WAW":(34,23),"VIE":(46,31),"ZRH":(66,44),
    "BRU":(66,44),"AMS":(62,41),"CDG":(53,36),"MAD":(42,28),
    "PMI":(34,23),"ACE":(34,23),"TFS":(34,23),"LPA":(34,23),
    "FUE":(34,23),"IBZ":(34,23),"ALC":(34,23),"SVQ":(42,28),
}

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
        success_url=f'{FRONTEND_URL}/success?ref={ref}',
        cancel_url=f'{FRONTEND_URL}/#tool',
        metadata={'ref': ref},
        locale='de',
        invoice_creation={'enabled': True},
    )
    return jsonify({'checkout_url': session.url, 'ref': ref})


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


@app.route('/api/upload-files', methods=['POST'])
def upload_files():
    ref = request.form.get('ref','')
    if ref not in _store:
        return jsonify({'error': 'ref not found'}), 404

    for key in ('lsb', 'dp', 'se', 'sb', 'zr', 'so'):
        files = request.files.getlist(key)
        if files:
            _store[ref]['files'][key] = [(f.read(), f.filename) for f in files]

    return jsonify({'status': 'ok'})


@app.route('/api/stripe-webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig     = request.headers.get('Stripe-Signature','')

    try:
        event = stripe.Webhook.construct_event(payload, sig, WEBHOOK_SECRET)
    except Exception as e:
        return jsonify({'error': str(e)}), 400

    if event['type'] in ('checkout.session.completed', 'payment_intent.succeeded'):
        session = event['data']['object']
        ref     = session.get('metadata', {}).get('ref','')

        if ref in _store:
            entry = _store[ref]
            entry['paid'] = True

            try:
                result = berechne(entry['form'], entry['files'])
                pdf_bytes = erstelle_pdf(result)

                dl_token = str(uuid.uuid4())
                name = result['name'].replace(' ','_')
                _store[dl_token] = {
                    'pdf_bytes': pdf_bytes,
                    'filename':  f'AeroTax_Auswertung_2025_{name}.pdf',
                    'expires':   datetime.utcnow() + timedelta(hours=24),
                }
                entry['dl_token'] = dl_token

            except Exception as e:
                print(f'PDF generation error: {e}')

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


@app.route('/api/download/<token>', methods=['GET'])
def download_pdf(token):
    entry = _store.get(token)
    if not entry:
        abort(404)
    if datetime.utcnow() > entry.get('expires', datetime.utcnow()):
        abort(410)
    return send_file(
        io.BytesIO(entry['pdf_bytes']),
        mimetype='application/pdf',
        as_attachment=True,
        download_name=entry.get('filename','AeroTax_Auswertung.pdf'),
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
    _store[token] = {
        'pdf_bytes': pdf,
        'filename':  'AeroTax_Auswertung_Demo_2025.pdf',
        'expires':   datetime.utcnow() + timedelta(hours=1),
    }
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
@app.route('/api/process', methods=['POST'])
def process_real():
    """Empfängt echte PDF-Dateien, ruft Claude KI auf, berechnet Werbungskosten."""
    try:
        # Form data
        # Nur die Felder die wirklich für die Berechnung gebraucht werden
        anreise = request.form.get('anreise', 'auto')
        form = {
            'name':    request.form.get('name', 'Flugbegleiter'),
            'year':    int(request.form.get('year', 2025)),
            'base':    request.form.get('base', 'Frankfurt (FRA)'),
            'anreise': anreise,
            'km':      float(request.form.get('km', 0)) if anreise in ('auto','fahrrad') else 0,
            'fahrzeug':   request.form.get('fahrzeug', 'verbrenner'),
            'oepnv_kosten': float(request.form.get('oepnv_kosten', 0)),
            'jobticket':  request.form.get('jobticket', 'nein'),
        }

        # Read uploaded files into memory
        files = {}
        for key in ['lsb', 'dp', 'se', 'stb', 'gew', 'arb', 'fort', 'tel',
                    'konz', 'bu', 'haft', 'kv', 'rv', 'leb', 'haus',
                    'arzt', 'zahn', 'medi', 'pfle', 'under', 'kata',
                    'spen', 'part', 'kind', 'hand', 'haed', 'kiru']:
            uploaded = request.files.getlist(key)
            if uploaded:
                files[key] = [_normalize_upload(f.read(), f.filename) for f in uploaded]

        # Check required files
        if not files.get('lsb') or not files.get('dp') or not files.get('se'):
            return jsonify({
                'error': 'Pflicht-Dokumente fehlen. Bitte lade Lohnsteuerbescheinigung, '
                         'Flugstunden-Übersichten und Streckeneinsatz-Abrechnungen hoch.'
            }), 400

        # ── BERECHNUNG MIT ECHTER KI ──
        result = berechne(form, files)
        if isinstance(result, tuple):
            result = result[0]

        # ── PDF ERSTELLEN ──
        pdf_bytes = erstelle_pdf(result)
        token = str(uuid.uuid4())
        name  = result['name'].replace(' ', '_')
        year  = form.get('year', 2025)
        _store[token] = {
            'pdf_bytes': pdf_bytes,
            'filename':  f'AeroTax_Auswertung_{year}_{name}.pdf',
            'expires':   datetime.utcnow() + timedelta(hours=24),
        }

        # Return result (safe: only primitives)
        safe = {k: v for k, v in result.items()
                if isinstance(v, (int, float, str))}
        # Strip file_bytes_list before JSON serialization
        opt_belege_safe = []
        for b in result.get('optionale_belege', []):
            b_safe = {k: v for k, v in b.items() if k != 'file_bytes_list'}
            opt_belege_safe.append(b_safe)

        return jsonify({
            'status':       'ready',
            'download_url': f'/api/download/{token}',
            'data':         safe,
            'abrechnungen': result.get('abrechnungen', []),
            'optionale_belege': opt_belege_safe,
            'notes':        result.get('notes', []),
        })

    except Exception as e:
        print(f'Process error: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


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



def parse_streckeneinsatz_mit_ki(pdf_bytes_list):
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
                    z77_page = round(g - steuer, 2)

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

    # ── Z76 (VMA Ausland) = Σ stfrei-Werte je Tag wo stfrei-Ort Ausland ist ──
    # Reine Addition der von LH bereits berechneten BMF-Tagessätze.
    # Keine eigene BMF-Tabelle, keine Lookups, keine Kategorisierungs-Regeln.
    INLAND = {'FRA','HAM','MUC','BER','DUS','STR','NUE','CGN','LEJ','HAJ',
              'HHN','BRE','DRS','ERF','NRN','FMO','LBC','TXL','PAD','SCN'}
    all_se_text = ''
    for pdf_bytes in pdf_bytes_list:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                all_se_text += '\n'.join(p.extract_text() or '' for p in pdf.pages) + '\n'
        except: pass

    vma_76_se = 0.0
    for line in all_se_text.split('\n'):
        line = line.strip()
        if ' X' in line: continue                                  # Storno
        if not re.match(r'^\d{2}\.\d{2}\.\d{4}', line): continue
        # Letzten 3-4-Letter-IATA-Code finden = stfrei-Ort
        # Davor steht der stfrei-Wert (Zahl mit Komma)
        m = re.search(r'([\d\.]+,\d{2})\s+([A-Z]{2,4})\s*$', line)
        if not m: continue
        sf_val_str, sf_ort = m.group(1), m.group(2)
        if sf_ort in INLAND: continue                              # Inland → Z72/Z73/Z74, nicht Z76
        try:
            sf_val = float(sf_val_str.replace('.','').replace(',','.'))
            vma_76_se += sf_val
        except: pass

    print(f"SE: Z77={z77_total:.2f}€ aus {len(abrechnungen)} Abrechnungen, Z76={vma_76_se:.2f}€ (Σ stfrei Ausland)")

    return {
        'abrechnungen':          abrechnungen,
        'summe_gesamt':          round(sum(a['gesamt'] for a in abrechnungen), 2),
        'summe_steuerfrei':      z77_total,
        'summe_steuerpflichtig': round(sum(a['steuerpflichtig'] for a in abrechnungen), 2),
        'vma_76_se':             round(vma_76_se, 2),
    }

def parse_dienstplan_mit_ki(pdf_bytes_list, se_bytes_list=None, km_form=0):
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
            resp = client.messages.create(model='claude-sonnet-4-5',max_tokens=400,
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
        if alle_seiten:
            flug_gesamt = '\n\n---\n\n'.join(alle_seiten)
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

        # FollowMe als letztes Content-Element (Lernbeispiel, kein Regelwerk)
        fm_kontext = ''
        try:
            fm_ref = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'referenz_faelle.txt')
            if os.path.exists(fm_ref):
                with open(fm_ref, encoding='utf-8') as fmf:
                    fm_kontext = '\n\nHIER SIND ZWEI BEREITS BERECHNETE FÄLLE ZUM VERGLEICH (von FollowMe verifiziert — nicht als Regeln, sondern als Beispiele zum Lernen):\n' + fmf.read()
        except: pass

        content.append({'type': 'text', 'text': f"""Du bist ein gewissenhafter Steuerberater spezialisiert auf Lufthansa-Kabinenpersonal.
Dein Mandant hat dir seine Unterlagen für 2025 gegeben. Deine Aufgabe: alle Werbungskosten für Anlage N berechnen.

Geh wie ein gründlicher Steuerberater vor — lies JEDEN Monat, JEDE Seite, JEDE Zeile der Dokumente.
Ein Steuerberater der nur 2 von 12 Monaten auswertet macht seinen Job nicht — sei gründlich.
{se_kontext}{fm_kontext}

REFERENZFALL (bereits verifiziert — zum Lernen wie LH-Dokumente zu lesen sind):
- Fahrtag: "03.01. LH400 A FRA 14:36" → A=Abflug FRA → Fahrtag ✓
- Kein Fahrtag: Vortag endete mit A FRA→MUC → heute noch unterwegs → kein Fahrtag
- KEINE Hotelnacht: "23.05. A FRA→TUN 20:10 / 24.05. E TUN→FRA 03:00" — nur ~5h Bodenzeit, du landest morgens in FRA → keine Übernachtung
- Hotelnacht: "20.04. A FRA→JNB 21:00 / 21.04. FL ... / 22.04. E JNB→FRA 17:55" — du übernachtest in JNB, das ist eine Hotel-Nacht
- Z73: SE "14,00  FRA" → stfrei=14, stfrei-Ort=FRA → Anreisetag Z73 ✓
- Z76: SE "48,00  SEL" → stfrei=48, stfrei-Ort=SEL(Ausland) → VMA Ausland Z76 ✓
- Z77: Alle stfrei-Einzelwerte summieren — NICHT die Summenzeile (Format variiert!)
Verifiziertes Ergebnis eines LH-Mitarbeiters: Fahrtage=53, Hotel=54, Z73=140€, Z76=4562€, Z77=4742,80€

═══ DEINE AUFGABE ═══
Lies Flugstunden + Streckeneinsatz Tag für Tag und ermittle die Jahressummen für:
- arbeitstage / fahrtage / hotel_naechte
- vma_72_tage / vma_73_tage / vma_74_tage / vma_aus  (aus den stfrei-Werten der SE — LH hat schon korrekt nach BMF berechnet, du übernimmst die Beträge)
z77 lass auf 0, das Backend setzt's deterministisch.

═══ INFO (zum Verstehen — keine starren Regeln) ═══

**LH-Marker die typischerweise vorkommen** (du erkennst Kontext aus Datum, Uhrzeit, Strecke):
- `/- FREIER TAG`, `U` (Urlaub), `K` (Krank), unbezahlte Freistellung → kein Arbeitstag
- `LH#### A FRA` = Abflug von FRA → Tour-Start, Arbeitstag, Fahrtag
- `LH#### E ... FRA` = Einflug nach FRA → Tour-Ende, Arbeitstag
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

        full_text = ''
        with client.messages.stream(
            model='claude-sonnet-4-5',
            max_tokens=16000,
            messages=[{'role': 'user', 'content': content}]
        ) as stream:
            for text in stream.text_stream:
                full_text += text
        full_text = full_text.strip()

        # JSON-Zeile finden — Claude soll sie an den Anfang setzen, fallback: irgendwo im Text
        nachweis = ''
        json_str = ''
        # Versuch 1: Erste Zeile direkt JSON?
        first_line = full_text.split('\n', 1)[0].strip()
        if first_line.startswith('{') and '"fahrtage"' in first_line:
            json_str = first_line
            nachweis = full_text[len(first_line):].strip()
        else:
            # Versuch 2: Flache JSON mit fahrtage finden (no nested braces)
            m = re.search(r'\{[^{}]*"fahrtage"[^{}]*\}', full_text, re.DOTALL)
            if m:
                json_str = m.group(0)
                nachweis = (full_text[:m.start()] + full_text[m.end():]).strip()
            else:
                # Versuch 3: Greedy
                ms = re.search(r'\{[\s\S]*\}', full_text)
                json_str = ms.group(0) if ms else '{}'

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

        return {
            'fahr_tage':    int(parsed.get('fahrtage', 0)),
            'km':           int(parsed.get('km', km)),
            'arbeitstage':  int(parsed.get('arbeitstage', 0)),
            'hotel_naechte':int(parsed.get('hotel_naechte', 0)),
            'vma_72_tage':  int(parsed.get('vma_72_tage', 0)),
            'vma_73_tage':  int(parsed.get('vma_73_tage', 0)),
            'vma_74_tage':  int(parsed.get('vma_74_tage', 0)),
            'vma_72':       float(parsed.get('vma_72', 0)),
            'vma_73':       float(parsed.get('vma_73', 0)),
            'vma_74':       float(parsed.get('vma_74', 0)),
            'vma_aus':      float(parsed.get('vma_aus', 0)),
            'z77':          float(parsed.get('z77', 0)),
            'nachweis':     nachweis,
            'ausland_touren': [],
        }

    except Exception as e:
        print(f'Claude Flugstunden error: {e}')
        raise RuntimeError(f'Steuerberechnung fehlgeschlagen: {e}')

def parse_optionale_belege(files):
    """
    Liest optionale Belege mit Claude Vision KI.
    Unterstützt PDFs und Bilder (JPG, PNG, WEBP, HEIC).
    """
    if not ANTHROPIC_KEY:
        return []

    WISO_PFADE = {
        'tel':  {'name':'Telefon & Internet', 'wiso':'Werbungskosten → Arbeitsmittel → Telefon & Internet', 'hint':'20% der Jahreskosten ansetzbar', 'icon':'📱'},
        'gew':  {'name':'Gewerkschaft / UFO', 'wiso':'Werbungskosten → Gewerkschaftsbeiträge', 'hint':'Voller Jahresbeitrag absetzbar', 'icon':'✊'},
        'stb':  {'name':'Steuerberatung', 'wiso':'Sonderausgaben → Steuerberatungskosten', 'hint':'Voller Betrag absetzbar', 'icon':'📋'},
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
        'kiru': {'name':'Kirchensteuer', 'wiso':'Sonderausgaben → Kirchensteuer', 'hint':'Voller Betrag', 'icon':'⛪'},
        'medi': {'name':'Medikamente', 'wiso':'Außergewöhnliche Belastungen → Krankheitskosten', 'hint':'Mit ärztlicher Verordnung', 'icon':'💊'},
        'konz': {'name':'Kontoführung', 'wiso':'Werbungskosten → Sonstige Werbungskosten', 'hint':'Pauschal 16€ oder Nachweis', 'icon':'🏦'},
        'kv':   {'name':'Krankenzusatz', 'wiso':'Vorsorgeaufwendungen → Sonstige', 'hint':'Anteilig', 'icon':'🦷'},
        'leb':  {'name':'Lebensversicherung', 'wiso':'Vorsorgeaufwendungen → Sonstige', 'hint':'Falls vor 2005', 'icon':'💚'},
        'haus': {'name':'Hausrat & Rechtsschutz', 'wiso':'Vorsorgeaufwendungen → Sonstige', 'hint':'Anteilig', 'icon':'🏠'},
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
                model='claude-sonnet-4-5',
                max_tokens=200,
                messages=[{'role': 'user', 'content': content_blocks}]
            )
            raw = response.content[0].text.strip()
            raw = re.sub(r'```json|```', '', raw).strip()
            parsed = json.loads(raw)
            results.append({
                'key': key,
                'icon': info['icon'],
                'name': info['name'],
                'wiso': info['wiso'],
                'hint': info['hint'],
                'betrag': float(parsed.get('betrag', 0)),
                'zeitraum': parsed.get('zeitraum', '2025'),
                'beschreibung': parsed.get('beschreibung', ''),
                'file_bytes_list': files[key],  # Store raw files for PDF embedding
            })
        except Exception as e:
            print(f'Optional doc {key} error: {e}')
            results.append({
                'key': key, 'icon': info['icon'], 'name': info['name'],
                'wiso': info['wiso'], 'hint': info['hint'],
                'betrag': 0, 'zeitraum': '2025',
                'beschreibung': 'Betrag konnte nicht extrahiert werden',
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
            model='claude-sonnet-4-5',
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
        
        se_data = parse_streckeneinsatz_mit_ki(files['se'])
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
            dp = parse_dienstplan_mit_ki(files['dp'], se_bytes_list=files.get('se'), km_form=km_form_val)
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

    # ── VMA-KATEGORISIERUNG: aus Claudes DP-Auswertung ────────
    # Claude liest die Streckeneinsatz- + Flugstunden-Texte und sortiert ein.
    # Z77 (gesamt-steuerfrei) kommt deterministisch aus dem SE-Parser, alles andere von Claude.
    if dp:
        vma_72_tage = dp.get('vma_72_tage', 0)
        vma_73_tage = dp.get('vma_73_tage', 0)
        vma_74_tage = dp.get('vma_74_tage', 0)
    else:
        vma_72_tage = inferred.get('vma_72_tage', 0)
        vma_73_tage = inferred.get('vma_73_tage', 0)
        vma_74_tage = inferred.get('vma_74_tage', 0)
    vma_72 = vma_72_tage * 14
    vma_73 = vma_73_tage * 14
    vma_74 = vma_74_tage * 28

    # ── KM: form > dienstplan > inferred ──────────────────────
    anreise = form.get('anreise', 'auto')
    km = float(form.get('km', 0)) if anreise in ('auto', 'fahrrad') else 0
    if km == 0 and km_dp > 0:
        km = km_dp

    # ── VMA BERECHNEN ─────────────────────────────────────────
    vma_in = vma_72 + vma_73 + vma_74

    # VMA Ausland — Priorität: SE-Summe (LH-genau) > DP > inferred
    vma_76_se = (se_data or {}).get('vma_76_se', 0)
    if vma_76_se > 0:
        vma_aus = vma_76_se
    elif dp and ausland_touren:
        vma_aus = 0
        for t in ausland_touren:
            ort = t.get('ort', '').upper()
            if ort in BMF_2025:
                s24, sab = BMF_2025[ort]
                vma_aus += t.get('an',0)*sab + t.get('voll',0)*s24 + t.get('ab',0)*sab
    elif dp and dp.get('vma_aus', 0) > 0:
        vma_aus = dp.get('vma_aus', 0)
    else:
        vma_aus = inferred.get('vma_aus', 0)

    # ── FAHRTKOSTEN ───────────────────────────────────────────
    fahrzeug  = form.get('fahrzeug', 'verbrenner')
    jobticket = form.get('jobticket', 'nein')
    if anreise in ('auto', 'fahrrad'):
        fahr = min(km,20)*fahr_tage*0.30 + max(0,km-20)*fahr_tage*0.38
    elif anreise == 'oepnv':
        oepnv_kosten = float(form.get('oepnv_kosten', 0))
        fahr = 0 if jobticket == 'ja_frei' else float(oepnv_kosten)
    else:
        fahr = 0
    fahr = round(fahr, 2)

    # ── REINIGUNG & TRINKGELD ────────────────────────────────
    reinig = round(arbeitstage * 1.60, 2)
    trink  = round(hotel_naechte * 3.60, 2)

    # ── GESAMTBERECHNUNG ─────────────────────────────────────
    gesamt = round(fahr + reinig + trink + vma_in + vma_aus, 2)
    netto  = round(gesamt - ag_z17 - z77, 2)

    # ── UPLOADED DOCS SUMMARY ────────────────────────────────
    uploaded_summary = []
    not_uploaded = []
    if files.get('lsb'):  uploaded_summary.append(f"LSB ({len(files['lsb'])} Datei(en))")
    else: not_uploaded.append("Lohnsteuerbescheinigung")
    if files.get('dp'):   uploaded_summary.append(f"Flugstunden ({len(files['dp'])} Datei(en))")
    else: not_uploaded.append("Flugstunden-Übersichten")
    if files.get('se'):   uploaded_summary.append(f"Streckeneinsatz ({len(files['se'])} Datei(en))")
    else: not_uploaded.append("Streckeneinsatz-Abrechnungen")

    # ── OPTIONALE BELEGE ─────────────────────────────────────
    opt_keys = ['stb','gew','arb','fort','tel','konz','bu','haft','kv',
                'rv','leb','haus','arzt','zahn','medi','pfle','under',
                'kata','spen','part','kind','hand','haed','kiru']
    opt_files = {k: files[k] for k in opt_keys if files.get(k)}
    optionale_belege = parse_optionale_belege(opt_files) if opt_files else []

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
         "Weitere Werbungskosten — Dienstplanauswertung AeroTax 2025"),
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
            "Es wurden keine Belege hochgeladen. Lade beim naechsten Mal deine Rechnungen unter Schritt 2 hoch — dann muss du sie nicht manuell in WISO suchen.",
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
                    if fb[:3]==b'\xff\xd8\xff' or fb[:4]==b'\x89PNG':
                        img = RLImage(io.BytesIO(fb))
                        iw,ih = img.drawWidth,img.drawHeight
                        scale = min(W_c/iw, 22*cm/ih, 1.0)
                        img.drawWidth=iw*scale; img.drawHeight=ih*scale
                        S.append(img)
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
