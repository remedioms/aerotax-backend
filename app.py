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
except ImportError:
    PIL_AVAILABLE = False
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor, white
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                 Table, TableStyle, PageBreak, HRFlowable)
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
            _store[ref]['files'][key] = [f.read() for f in files]

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
                files[key] = [f.read() for f in uploaded]

        # Check required files
        if not files.get('lsb') or not files.get('dp') or not files.get('se'):
            return jsonify({
                'error': 'Pflicht-Dokumente fehlen. Bitte lade Lohnsteuerbescheinigung, '
                         'Flugstunden-Übersichten und Streckeneinsatz-Abrechnungen hoch.'
            }), 400

        # ── BERECHNUNG MIT ECHTER KI ──
        result = berechne(form, files)

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


# ══════════════════════════════════════════════════════════════════
#  KI-PARSER — liest die Lufthansa PDFs
# ══════════════════════════════════════════════════════════════════

def parse_lohnsteuerbescheinigung(pdf_bytes_list):
    """Extrahiert alle relevanten Werte aus der Lohnsteuerbescheinigung."""
    result = {
        'brutto':0,'lohnsteuer':0,'soli':0,'ag_fahrt_z17':0,
        'rv_an':0,'kv_an':0,'pv_an':0,'av_an':0,'identnr':'',
        'arbeitgeber':'Deutsche Lufthansa AG',
    }
    for pdf_bytes in pdf_bytes_list:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = '\n'.join(p.extract_text() or '' for p in pdf.pages)

            def find(pattern, default=0):
                m = re.search(pattern, text, re.IGNORECASE|re.DOTALL)
                if m:
                    try: return float(m.group(1).replace('.','').replace(',','.'))
                    except: pass
                return default

            b = find(r'Bruttoarbeitslohn[^\d]+([\d\.]+,\d{2})')
            if b > 0:
                result['brutto']       = b
                result['lohnsteuer']   = find(r'Lohnsteuer von 3\.[^\d]+([\d\.]+,\d{2})')
                result['soli']         = find(r'Solidarit[^\d]+([\d\.]+,\d{2})')
                result['ag_fahrt_z17'] = find(r'Entfernungspauschale anzurechnen sind\s+([\d\.]+,\d{2})')
                result['rv_an']        = find(r'\d{2}\.[^\d]+versicherung\s+([\d\.]+,\d{2})\nanteil')
                result['kv_an']        = find(r'Arbeitnehmerbeitr[^\d]+Kranken[^\d]+([\d\.]+,\d{2})')
                result['pv_an']        = find(r'Arbeitnehmerbeitr[^\d]+Pflege[^\d]+([\d\.]+,\d{2})')
                result['av_an']        = find(r'Arbeitslosenversicherung[^\d]+([\d\.]+,\d{2})')
                id_m = re.search(r'(\d{11})', text)
                if id_m: result['identnr'] = id_m.group(1)
        except Exception as e:
            print(f'LSt parse error: {e}')
    return result


def parse_streckeneinsatz_mit_ki(pdf_bytes_list):
    """
    Liest Streckeneinsatz-Abrechnungen.
    1. Versucht Regex-Extraktion (Text-PDFs)
    2. Fallback: Claude Vision fuer gescannte/Image PDFs
    """
    if not pdf_bytes_list:
        return None

    abrechnungen = []
    image_only_pages = []  # pages where text extraction failed

    for pdf_bytes in pdf_bytes_list:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    text = page.extract_text() or ''

                    if text.strip():
                        # Text PDF — use regex
                        date_m = re.search(r'Erstellt\s+(\d{2}\.\d{2}\.\d{4})', text)
                        erstellt = date_m.group(1) if date_m else ''

                        monat_m = re.search(r'Erstellt\s+\d{2}\.(\d{2})\.\d{4}', text)
                        monat_nr = int(monat_m.group(1)) if monat_m else len(abrechnungen)+1
                        try:
                            monat_name = __import__('datetime').date(2025, monat_nr, 1).strftime('%B')
                        except:
                            monat_name = f'Monat {monat_nr:02d}'

                        summe_m = re.search(r'Summe:\s+([\d\.]+,[\d]+)\s+([\d\.]+,[\d]+)(?:\s+([\d\.]+,[\d]+))?', text)
                        if summe_m:
                            to_f = lambda s: float(s.replace('.','').replace(',','.')) if s else 0.0
                            g = to_f(summe_m.group(1))
                            v3 = to_f(summe_m.group(3)) if summe_m.group(3) else 0.0
                            steuer = v3 if v3 > 0 else to_f(summe_m.group(2))
                            abrechnungen.append({
                                'erstellt': erstellt,
                                'bezeichnung': monat_name,
                                'gesamt': g,
                                'steuerpflichtig': steuer,
                                'steuerfrei': max(0, round(g - steuer, 2))
                            })
                    else:
                        # Image-based page — collect for Claude Vision
                        image_only_pages.append((pdf_bytes, len(abrechnungen)))

        except Exception as e:
            print(f'SE parse error: {e}')

    # If image pages found and Claude available, use Vision
    if image_only_pages and ANTHROPIC_KEY:
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

            # Convert PDF pages to images via pdf2image or just send raw PDF
            content_blocks = []
            for pdf_bytes, _ in image_only_pages[:6]:  # limit to 6 files
                b64 = base64.standard_b64encode(pdf_bytes).decode('utf-8')
                content_blocks.append({
                    'type': 'document',
                    'source': {
                        'type': 'base64',
                        'media_type': 'application/pdf',
                        'data': b64
                    }
                })

            content_blocks.append({
                'type': 'text',
                'text': """Diese PDFs sind Lufthansa Streckeneinsatz-Abrechnungen.
Extrahiere fuer JEDE Abrechnung:
- Erstellungsdatum (TT.MM.JJJJ)
- Monat (z.B. Januar, Februar)
- Summe Gesamt (letzte Zeile "Summe: X")
- Davon steuerpflichtig
- Steuerfrei = Gesamt - Steuerpflichtig

Antworte NUR mit JSON:
{"abrechnungen":[{"erstellt":"13.02.2025","bezeichnung":"Januar","gesamt":244.80,"steuerpflichtig":63.80,"steuerfrei":181.00}],"summe_gesamt":0,"summe_steuerpflichtig":0,"summe_steuerfrei":0}"""
            })

            response = client.messages.create(
                model='claude-sonnet-4-20250514',
                max_tokens=2000,
                messages=[{'role': 'user', 'content': content_blocks}]
            )
            raw = re.sub(r'```json|```', '', response.content[0].text.strip()).strip()
            vision_data = json.loads(raw)
            abrechnungen.extend(vision_data.get('abrechnungen', []))
            print(f"Claude Vision extracted {len(vision_data.get('abrechnungen',[]))} months from image PDFs")

        except Exception as e:
            print(f'SE Vision fallback error: {e}')

    if not abrechnungen:
        return None

    summe_gesamt = round(sum(a['gesamt'] for a in abrechnungen), 2)
    summe_steuer = round(sum(a['steuerpflichtig'] for a in abrechnungen), 2)
    summe_frei   = round(sum(a['steuerfrei'] for a in abrechnungen), 2)

    return {
        'abrechnungen': abrechnungen,
        'summe_gesamt': summe_gesamt,
        'summe_steuerpflichtig': summe_steuer,
        'summe_steuerfrei': summe_frei,
    }


def parse_dienstplan_mit_ki(pdf_bytes_list):
    from datetime import date
    """
    Liest Flugstunden-Übersichten / Dienstplanauswertung.
    Strategie: Regex-Extraktion zuerst (zuverlässig), Claude als Fallback.
    Unterstützt Lufthansa Flugstunden-Übersichten UND FollowMe Dienstplanauswertung.
    """
    if not pdf_bytes_list:
        return None

    # Combine all text
    all_text = []
    for pdf_bytes in pdf_bytes_list:
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                all_text.append(text)
        except Exception as e:
            print(f'Dienstplan read error: {e}')
    
    if not all_text:
        return None
    
    combined = '\n'.join(all_text)
    
    def find(pattern, default=0):
        m = re.search(pattern, combined, re.IGNORECASE|re.DOTALL)
        if m:
            try: return float(m.group(1).replace('.','').replace(',','.'))
            except: pass
        return default
    
    def find_int(pattern, default=0):
        m = re.search(pattern, combined, re.IGNORECASE)
        if m:
            try: return int(m.group(1).replace('.',''))
            except: pass
        return default

    result = {}

    # ── FOLLOWME DIENSTPLANAUSWERTUNG (preferred — has pre-calculated values) ──
    if 'FollowMe' in combined or 'Dienstplanauswertung' in combined:
        result = {
            'fahr_tage':     find_int(r'aufgesucht an\s+(\d+)\s+Tagen'),
            'km':            find_int(r'aufgesucht an\s+\d+\s+Tagen\s+(\d+)\s+km'),
            'arbeitstage':   find_int(r'Arbeitstage:\s+(\d+)'),
            'hotel_naechte': find_int(r'Hotelaufenthalte:\s+(\d+)'),
            'vma_72_tage':   find_int(r'Zeile 72[^\d]+(\d+)\s+Tage'),
            'vma_73_tage':   find_int(r'Zeile 73[^\d]+(\d+)\s+Tage'),
            'vma_74_tage':   find_int(r'Zeile 74[^\d]+(\d+)\s+Tag'),
            'vma_72':        find(r'Zeile 72[^€]+\s+(\d+)\s+Tage\s+([\d\.]+,\d{2})\s*€'),
            'vma_73':        find(r'Zeile 73[^\d]+\d+\s+Tage\s+([\d\.]+,\d{2})\s*€'),
            'vma_74':        find(r'Zeile 74[^\d]+\d+\s+Tag\s+([\d\.]+,\d{2})\s*€'),
            'vma_aus':       find(r'Zeile 76[^\d]+([\d\.]+,\d{2})\s*€'),
            'ausland_touren': [],
        }
        # vma_72 needs special handling (pattern returns second group)
        m72 = re.search(r'Zeile 72[^\d]+(\d+)\s+Tage\s+([\d\.]+,\d{2})\s*€', combined, re.IGNORECASE)
        if m72:
            result['vma_72_tage'] = int(m72.group(1))
            result['vma_72'] = float(m72.group(2).replace('.','').replace(',','.'))
        m74 = re.search(r'Zeile 74[^\d]+(\d+)\s+Tag[^\d]+([\d\.]+,\d{2})\s*€', combined, re.IGNORECASE)
        if m74:
            result['vma_74_tage'] = int(m74.group(1))
            result['vma_74'] = float(m74.group(2).replace('.','').replace(',','.'))
        
        if result.get('arbeitstage', 0) > 0:
            return result

    # ── LUFTHANSA FLUGSTUNDEN-ÜBERSICHTEN (raw data) ──
    # Count from flight lines
    arbeitstage = len(re.findall(r'\d{2}\.\d{2}\.\s+(?:LH|4U|EW|OS|DE)\d+', combined))
    hotel_naechte = len(re.findall(r'FL\s+STRECKENEINSATZTAG', combined))
    fahr_tage = len(re.findall(r'\d{2}\.\d{2}\.\s+(?:LH|4U|EW|OS|DE)\d+.*?\d{2}:\d{2}', combined))
    
    if arbeitstage > 0:
        # For VMA and km — use Claude since raw Flugstunden don't have summaries
        if ANTHROPIC_KEY:
            try:
                client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
                sample = combined[:12000]
                prompt = f"""Analysiere diese Flugstunden-Übersichten und berechne:
{sample}

Zähle für das Gesamtjahr:
1. Arbeitstage (Tage mit Flugdienst LH/4U/OS/EW/DE)
2. Hotel-Nächte (FL STRECKENEINSATZTAG Zeilen)  
3. Fahrt-Tage (Tage mit Abflug = erste Zeile einer Dienstreise)
4. VMA >8h Inland (Eintagestouren ohne Übernachtung) = Zeile 72
5. An/Abreisetage mit Übernachtung = Zeile 73
6. 24h Inlandstage = Zeile 74
7. Homebase km (Wohnort zur Homebase, falls erkennbar)

Antworte NUR mit JSON:
{{"arbeitstage":133,"fahr_tage":58,"hotel_naechte":66,"vma_72_tage":5,"vma_73_tage":11,"vma_74_tage":1,"vma_72":70,"vma_73":154,"vma_74":28,"vma_aus":4794,"km":28,"ausland_touren":[]}}"""
                response = client.messages.create(
                    model='claude-sonnet-4-20250514', max_tokens=500,
                    messages=[{'role':'user','content':prompt}]
                )
                raw = re.sub(r'```json|```','', response.content[0].text.strip()).strip()
                return json.loads(raw)
            except Exception as e:
                print(f'Dienstplan Claude error: {e}')
        
        # Pure regex fallback
        return {
            'arbeitstage': arbeitstage,
            'fahr_tage': fahr_tage,
            'hotel_naechte': hotel_naechte,
            'vma_72_tage': 0, 'vma_73_tage': 0, 'vma_74_tage': 0,
            'vma_72': 0, 'vma_73': 0, 'vma_74': 0, 'vma_aus': 0,
            'km': 0, 'ausland_touren': [],
        }
    
    return None


def parse_optionale_belege(files):
    """
    Liest optionale Belege mit Claude Vision KI.
    Unterstützt PDFs und Bilder (JPG, PNG, WEBP).
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
        """Converts file bytes to Claude message content (text or image)."""
        # Detect file type
        ext = filename.lower().split('.')[-1] if '.' in filename else ''
        
        # Try as image first (JPG, PNG, WEBP, GIF)
        img_types = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 
                     'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'}
        if ext in img_types:
            b64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            return {
                'type': 'image',
                'source': {'type': 'base64', 'media_type': img_types[ext], 'data': b64}
            }
        
        # Check magic bytes for image
        if file_bytes[:3] == b'\xff\xd8\xff':  # JPEG
            b64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            return {'type':'image','source':{'type':'base64','media_type':'image/jpeg','data':b64}}
        if file_bytes[:8] == b'\x89PNG\r\n\x1a\n':  # PNG
            b64 = base64.standard_b64encode(file_bytes).decode('utf-8')
            return {'type':'image','source':{'type':'base64','media_type':'image/png','data':b64}}
        
        # Try as PDF
        try:
            with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                text = ' '.join(p.extract_text() or '' for p in pdf.pages)
                return {'type': 'text', 'text': text[:3000]}
        except:
            pass
        
        # Fallback: try as plain text
        try:
            return {'type': 'text', 'text': file_bytes.decode('utf-8', errors='ignore')[:3000]}
        except:
            return {'type': 'text', 'text': '[Datei konnte nicht gelesen werden]'}

    for key, info in WISO_PFADE.items():
        if not files.get(key):
            continue

        content_blocks = []
        for i, file_bytes in enumerate(files[key]):
            block = file_to_claude_content(file_bytes)
            content_blocks.append(block)

        if not content_blocks:
            continue

        content_blocks.append({
            'type': 'text',
            'text': f"""Analysiere diesen Beleg für: {info['name']}
WISO-Eintrag: {info['wiso']}

Extrahiere:
1. Den relevanten Jahresbetrag (Gesamtsumme)
2. Zeitraum (z.B. "2025" oder "Jan-Dez 2025")
3. Kurze Beschreibung (max. 8 Wörter)

Antworte NUR mit JSON (keine Backticks):
{{"betrag": 245.80, "zeitraum": "2025", "beschreibung": "Monatliche Beiträge 2025"}}"""
        })

        try:
            response = client.messages.create(
                model='claude-sonnet-4-20250514',
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



def infer_missing_data_with_ki(files, available_data, missing):
    """
    When documents are missing or incomplete, Claude infers values
    from available documents. Always tries to be accurate using cross-references.
    Returns dict with inferred values and notes about what was estimated.
    """
    if not ANTHROPIC_KEY:
        return {}, []

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    notes = []  # Will be shown in PDF as warnings
    inferred = {}

    # Build context from available data
    context_parts = []
    
    if available_data.get('lsb_text'):
        context_parts.append(f"LOHNSTEUERBESCHEINIGUNG:\n{available_data['lsb_text'][:2000]}")
    if available_data.get('se_text'):
        context_parts.append(f"STRECKENEINSATZ-ABRECHNUNGEN (vorhandene Monate):\n{available_data['se_text'][:4000]}")
    if available_data.get('dp_text'):
        context_parts.append(f"FLUGSTUNDEN-ÜBERSICHTEN (vorhandene Monate):\n{available_data['dp_text'][:4000]}")
    
    if not context_parts:
        return {}, ['Zu wenige Dokumente für Schätzung vorhanden.']

    context = '\n\n'.join(context_parts)
    
    missing_str = ', '.join(missing)
    
    prompt = f"""Du bist ein Steuerexperte für Lufthansa-Flugbegleiter.

Folgende Dokumente sind VORHANDEN:
{context}

Folgendes FEHLT oder konnte nicht gelesen werden: {missing_str}

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
            model='claude-sonnet-4-20250514',
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
        for pdf_bytes in files['lsb']:
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
        for pdf_bytes in files['se']:
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
                missing.append(f'Streckeneinsatz: nur {months_found} von 12 Monaten gefunden')
    else:
        missing.append('Streckeneinsatz-Abrechnungen (nicht hochgeladen)')

    # ── FLUGSTUNDEN-ÜBERSICHTEN ───────────────────────────────
    dp = None
    if files.get('dp'):
        # Collect raw text
        dp_texts = []
        for pdf_bytes in files['dp']:
            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    dp_texts.append('\n'.join(p.extract_text() or '' for p in pdf.pages))
            except: pass
        if dp_texts:
            available_texts['dp_text'] = '\n'.join(dp_texts)
        
        dp = parse_dienstplan_mit_ki(files['dp'])
        if not dp or not dp.get('arbeitstage'):
            missing.append('Flugstunden-Übersichten (nicht lesbar)')
            dp = None
    else:
        missing.append('Flugstunden-Übersichten (nicht hochgeladen)')

    # ── SMART INFERENCE für fehlende Daten ────────────────────
    inferred = {}
    if missing and available_texts:
        print(f"Running inference for missing: {missing}")
        inferred, inf_notes = infer_missing_data_with_ki(files, available_texts, missing)
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
        ag_z17        = lst.get('ag_fahrt_z17', 0)
        brutto        = lst.get('brutto', 0)
        lohnsteuer    = lst.get('lohnsteuer', 0)
        arbeitgeber   = lst.get('arbeitgeber', 'Deutsche Lufthansa AG')
    else:
        ag_z17        = inferred.get('ag_z17', 0)
        brutto        = inferred.get('brutto', 0)
        lohnsteuer    = inferred.get('lohnsteuer', 0)
        arbeitgeber   = 'Deutsche Lufthansa AG'
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

    # Dienstplan values
    if dp:
        arbeitstage    = dp.get('arbeitstage', 0)
        fahr_tage      = dp.get('fahr_tage', 0)
        hotel_naechte  = dp.get('hotel_naechte', 0)
        vma_72_tage    = dp.get('vma_72_tage', 0)
        vma_73_tage    = dp.get('vma_73_tage', 0)
        vma_74_tage    = dp.get('vma_74_tage', 0)
        ausland_touren = dp.get('ausland_touren', [])
        km_dp          = dp.get('km', 0)
    else:
        arbeitstage    = inferred.get('arbeitstage', 0)
        fahr_tage      = inferred.get('fahr_tage', 0)
        hotel_naechte  = inferred.get('hotel_naechte', 0)
        vma_72_tage    = inferred.get('vma_72_tage', 0)
        vma_73_tage    = inferred.get('vma_73_tage', 0)
        vma_74_tage    = inferred.get('vma_74_tage', 0)
        ausland_touren = []
        km_dp          = inferred.get('km', 0)
        if not arbeitstage:
            notes.append('⚠️ Flugstunden-Übersichten fehlen — Arbeitstage und VMA konnten nicht berechnet werden.')

    # ── KM: form > dienstplan > inferred ──────────────────────
    anreise = form.get('anreise', 'auto')
    km = float(form.get('km', 0)) if anreise in ('auto', 'fahrrad') else 0
    if km == 0 and km_dp > 0:
        km = km_dp

    # ── VMA BERECHNEN ─────────────────────────────────────────
    vma_72 = vma_72_tage * 14
    vma_73 = vma_73_tage * 14
    vma_74 = vma_74_tage * 28
    vma_in = vma_72 + vma_73 + vma_74

    # VMA Ausland
    if dp and ausland_touren:
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
        'notes':            notes,  # Hinweise über geschätzte Werte
        'datum':            datetime.now().strftime('%d.%m.%Y'),
        'km':               km,
        'arbeitstage':      arbeitstage,
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
        'reinig':           reinig,
        'trink':            trink,
        'gesamt':           gesamt,
        'ag_z17':           ag_z17,
        'spesen_gesamt':    spesen_gesamt,
        'spesen_steuer':    spesen_steuer,
        'z77':              z77,
        'netto':            netto,
        'abrechnungen':     abrechnungen,
        'brutto':           brutto,
        'lohnsteuer':       lohnsteuer,
        'arbeitgeber':      arbeitgeber,
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
    S.append(Paragraph(
        f"Erstellt am {d.get('datum','')}  ·  AeroTAX  ·  aerosteuer.de",
        ps("cf", fontSize=8, textColor=TEXT3, fontName="Helvetica",
           leading=12, alignment=TA_CENTER)))




    # ════════════════════════════════════════════════

    # Fehlende Dokumente — unten auf Deckblatt
    _not_upl = d.get('not_uploaded', '')
    if _not_upl and 'Alle Pflichtdokumente' not in _not_upl:
        S.append(Spacer(1, 0.4*cm))
        S.append(HRFlowable(width='100%', thickness=0.3, color=LINE, spaceAfter=6))
        S.append(Paragraph(
            f'Fehlende Dokumente: {_not_upl}',
            ps('warn_miss', fontSize=8, textColor=TEXT2,
               fontName='Helvetica', leading=12)))
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
            for fidx, fb in enumerate(fbl):
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
                        img = Image(io.BytesIO(fb))
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
                except:
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
    # LETZTE SEITE — BESTÄTIGUNG
    # ════════════════════════════════════════════════
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
