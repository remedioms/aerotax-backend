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
ANTHROPIC_KEY         = os.getenv('ANTHROPIC_API_KEY')
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

    if event['type'] == 'checkout.session.completed':
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
    """Demo mit zufälligen Beispielzahlen — keine echten Nutzerdaten, kein Tibor."""
    import random
    r = lambda a, b: round(random.uniform(a, b), 2)
    ri = lambda a, b: random.randint(a, b)

    km          = ri(15, 60)
    fahr_tage   = ri(45, 70)
    arbeitstage = ri(110, 150)
    hotel_naechte = ri(50, 80)
    vma_72      = ri(3, 8) * 14
    vma_73      = ri(8, 15) * 14
    vma_74      = ri(0, 2) * 28
    vma_in      = vma_72 + vma_73 + vma_74
    vma_aus     = r(3500, 6000)
    fahr        = round(min(km,20)*fahr_tage*0.30 + max(0,km-20)*fahr_tage*0.38, 2)
    reinig      = round(arbeitstage * 1.60, 2)
    trink       = round(hotel_naechte * 3.60, 2)
    gesamt      = round(fahr + reinig + trink + vma_in + vma_aus, 2)
    ag_z17      = r(200, 450)
    spesen_g    = r(4000, 7000)
    spesen_s    = r(800, 2000)
    z77         = round(spesen_g - spesen_s, 2)
    netto       = round(gesamt - ag_z17 - z77, 2)

    result = {
        'name': 'Max Mustermann',
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
        'brutto': r(40000, 70000), 'lohnsteuer': r(5000, 12000),
        'arbeitgeber': 'Deutsche Lufthansa AG',
        'uploaded_summary': 'Demo-Modus — keine echten Dokumente',
        'not_uploaded': '',
        'abrechnungen': [
            {'erstellt': f'{m:02d}.2025', 'bezeichnung': f'Monat {m}',
             'gesamt': round(spesen_g/12, 2),
             'steuerpflichtig': round(spesen_s/12, 2),
             'steuerfrei': round((spesen_g-spesen_s)/12, 2)}
            for m in range(1, 13)
        ],
    }

    pdf   = erstelle_pdf(result)
    token = str(uuid.uuid4())
    _store[token] = {
        'pdf_bytes': pdf,
        'filename':  'AeroTax_Demo_Auswertung.pdf',
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
                if isinstance(v, (int, float, str)) and k != 'abrechnungen'}
        return jsonify({
            'status':       'ready',
            'download_url': f'/api/download/{token}',
            'data':         safe
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
                result['lohnsteuer']   = find(r'Lohnsteuer[^\d]+([\d\.]+,\d{2})')
                result['soli']         = find(r'Solidarit[^\d]+([\d\.]+,\d{2})')
                result['ag_fahrt_z17'] = find(r'(?:Zeile 17|Entfernungspauschale)[^\d]+([\d\.]+,\d{2})')
                result['rv_an']        = find(r'Rentenversicherung[^\d]+([\d\.]+,\d{2})')
                result['kv_an']        = find(r'Krankenversicherung[^\d]+([\d\.]+,\d{2})')
                result['pv_an']        = find(r'Pflegeversicherung[^\d]+([\d\.]+,\d{2})')
                result['av_an']        = find(r'Arbeitslosenversicherung[^\d]+([\d\.]+,\d{2})')
                id_m = re.search(r'(\d{11})', text)
                if id_m: result['identnr'] = id_m.group(1)
        except Exception as e:
            print(f'LSt parse error: {e}')
    return result


def parse_streckeneinsatz_mit_ki(pdf_bytes_list):
    """
    Liest alle Streckeneinsatz-Abrechnungen mit Claude KI.
    Extrahiert: Gesamt-Spesen, davon Steuerpflichtig
    Zeile 77 = Gesamt - Steuerpflichtig
    """
    if not pdf_bytes_list:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    # Text aus allen PDFs zusammenführen
    all_texts = []
    for i, pdf_bytes in enumerate(pdf_bytes_list):
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                all_texts.append(f"=== Abrechnung {i+1} ===\n{text[:3000]}")
        except: pass

    if not all_texts:
        return []

    combined = '\n\n'.join(all_texts)

    prompt = f"""Du bist ein Steuerexperte für Lufthansa-Flugbegleiter.

Hier sind die Streckeneinsatz-Abrechnungen (Monatsabrechnungen der Spesen):

{combined[:15000]}

Extrahiere für JEDE Abrechnung:
1. Erstellungsdatum (Format TT.MM.JJJJ)
2. Beschreibung / Zeitraum (z.B. welche Monate/Rotationen)
3. Gesamt-Spesen (Bruttogesamtbetrag)
4. Davon steuerpflichtig (der Teil der versteuert wird)
5. Steuerfrei = Gesamt - Steuerpflichtig

WICHTIG: "Steuerfrei" ist was als Zeile 77 in WISO abzuziehen ist.

Antworte NUR mit JSON, keine Backticks, kein Markdown:
{{
  "abrechnungen": [
    {{
      "erstellt": "13.02.2025",
      "bezeichnung": "Januar (HKG-Rotation, DEN-Umlauf)",
      "gesamt": 244.80,
      "steuerpflichtig": 33.60,
      "steuerfrei": 211.20
    }}
  ],
  "summe_gesamt": [Summe aller Gesamt-Spesen],
  "summe_steuerpflichtig": [Summe steuerpflichtig],
  "summe_steuerfrei": [Summe steuerfrei]
}}"""

    try:
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=2000,
            messages=[{'role':'user','content':prompt}]
        )
        text_resp = response.content[0].text.strip()
        text_resp = re.sub(r'```json|```','',text_resp).strip()
        data = json.loads(text_resp)
        return data
    except Exception as e:
        print(f'Streckeneinsatz KI error: {e}')
        return None


def parse_dienstplan_mit_ki(pdf_bytes_list):
    """
    Liest Flugstunden-Übersichten mit Claude KI.
    Extrahiert: Arbeitstage, Hotelübernachtungen, VMA-Tage, Auslands-Touren.
    """
    if not pdf_bytes_list:
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    all_texts = []
    for i, pdf_bytes in enumerate(pdf_bytes_list):
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                text = '\n'.join(p.extract_text() or '' for p in pdf.pages)
                all_texts.append(f"=== Monat {i+1} ===\n{text[:4000]}")
        except: pass

    if not all_texts:
        return None

    combined = '\n\n'.join(all_texts)

    prompt = f"""Du bist ein Steuerexperte für Lufthansa-Flugbegleiter.

Hier sind die Flugstunden-Übersichten (Dienstpläne) für 2025:

{combined[:18000]}

Extrahiere folgende Werte für das Gesamtjahr:

ARBEITSTAGE: Alle Tage mit Flugeinsatz oder Streckeneinsatztag (FL)
FAHRT-TAGE: Tage an denen der Mitarbeiter zur Homebase gefahren ist (= Dienst-Starttage ohne FL-Vortag)
HOTEL-NÄCHTE: FL-Tage = Übernachtungen im Ausland (Streckeneinsatztage)
VMA INLAND:
  - Zeile 72: Tage mit >8h Abwesenheit OHNE Übernachtung (Eintagestouren Ausland oder lange Inlandstouren)
  - Zeile 73: An- und Abreisetage bei mehrtägigen Einsätzen MIT Übernachtung
  - Zeile 74: Volle 24h-Tage im Inland
VMA AUSLAND: Für jeden Auslandseinsatz MIT Übernachtung den IATA-Code und Tagestypen:
  - an = Anreisetag (Tagessatz: An-/Abreisetag-Pauschale)
  - voll = volle 24h-Tage (Tagessatz: voller Tagessatz)
  - ab = Abreisetag (Tagessatz: An-/Abreisetag-Pauschale)

WICHTIG: Eintagestouren ohne Übernachtung = Inland Zeile 72, NICHT Ausland!

Antworte NUR mit JSON:
{{
  "arbeitstage": [Anzahl aus PDFs],
  "fahr_tage": [Anzahl aus PDFs],
  "hotel_naechte": [Anzahl aus PDFs],
  "vma_72_tage": 5,
  "vma_73_tage": 11,
  "vma_74_tage": 1,
  "ausland_touren": [
    {{"ort": "HKG", "an": 1, "voll": 3, "ab": 1}},
    {{"ort": "BOM", "an": 1, "voll": 2, "ab": 1}},
    {{"ort": "CPH", "an": 1, "voll": 0, "ab": 0}}
  ]
}}"""

    try:
        response = client.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=3000,
            messages=[{'role':'user','content':prompt}]
        )
        text_resp = response.content[0].text.strip()
        text_resp = re.sub(r'```json|```','',text_resp).strip()
        return json.loads(text_resp)
    except Exception as e:
        print(f'Dienstplan KI error: {e}')
        return None


# ══════════════════════════════════════════════════════════════════
#  HAUPTBERECHNUNG
#  Exakt nach FollowMe-Methode:
#  1. Brutto-Aufwendungen ermitteln
#  2. AG-Erstattungen (Z17 + Z77) abziehen
#  3. Netto-Betrag = in WISO unter Reisenebenkosten eintragen
# ══════════════════════════════════════════════════════════════════

def berechne(form, files):
    """
    Berechnet alle Werbungskosten.
    WICHTIG: Keine Fallback-Werte auf Tibor-Daten.
    Wenn KI PDFs nicht lesen kann → Exception → Fehlermeldung an Nutzer.
    """

    # ── LOHNSTEUERBESCHEINIGUNG ────────────────────────────────────
    if files.get('lsb') and ANTHROPIC_KEY:
        lst = parse_lohnsteuerbescheinigung(files['lsb'])
        if not lst.get('brutto'):
            raise ValueError(
                'Lohnsteuerbescheinigung konnte nicht ausgelesen werden. '
                'Bitte stelle sicher dass es eine lesbare PDF ist.'
            )
    else:
        raise ValueError('Lohnsteuerbescheinigung fehlt oder ANTHROPIC_KEY nicht gesetzt.')
    ag_z17 = lst['ag_fahrt_z17']

    # ── STRECKENEINSATZ-ABRECHNUNGEN ──────────────────────────────
    if files.get('se') and ANTHROPIC_KEY:
        se_data = parse_streckeneinsatz_mit_ki(files['se'])
        if se_data and isinstance(se_data, dict) and se_data.get('summe_gesamt', 0) > 0:
            abrechnungen  = se_data.get('abrechnungen', [])
            spesen_gesamt = se_data.get('summe_gesamt', 0)
            spesen_steuer = se_data.get('summe_steuerpflichtig', 0)
            z77           = se_data.get('summe_steuerfrei', spesen_gesamt - spesen_steuer)
        else:
            raise ValueError(
                'Streckeneinsatz-Abrechnungen konnten nicht ausgelesen werden. '
                'Bitte alle 12 Monate hochladen und sicherstellen dass es lesbare PDFs sind.'
            )
    else:
        raise ValueError('Streckeneinsatz-Abrechnungen fehlen oder ANTHROPIC_KEY nicht gesetzt.')

    # ── DIENSTPLAN / FLUGSTUNDEN-ÜBERSICHTEN ─────────────────────
    if files.get('dp') and ANTHROPIC_KEY:
        dp = parse_dienstplan_mit_ki(files['dp'])
        if not dp or not dp.get('arbeitstage'):
            raise ValueError(
                'Flugstunden-Übersichten konnten nicht ausgelesen werden. '
                'Bitte alle 12 Monate hochladen.'
            )
    else:
        raise ValueError('Flugstunden-Übersichten fehlen oder ANTHROPIC_KEY nicht gesetzt.')

    arbeitstage   = dp.get('arbeitstage', 0)
    fahr_tage     = dp.get('fahr_tage', 0)
    hotel_naechte = dp.get('hotel_naechte', 0)
    vma_72_tage   = dp.get('vma_72_tage', 0)
    vma_73_tage   = dp.get('vma_73_tage', 0)
    vma_74_tage   = dp.get('vma_74_tage', 0)
    ausland_touren = dp.get('ausland_touren', [])

    # VMA Ausland nach BMF-Pauschalen 2025
    vma_aus = 0
    for t in ausland_touren:
        ort = t.get('ort', '').upper()
        if ort in BMF_2025:
            s24, sab = BMF_2025[ort]
            vma_aus += t.get('an',0)*sab + t.get('voll',0)*s24 + t.get('ab',0)*sab

    # ── AUFWENDUNGEN BERECHNEN ────────────────────────────────────
    # Fahrtkosten: NUR einfache Strecke! Abhängig von Anreiseart.
    anreise = form.get('anreise', 'auto')
    km      = float(form.get('km', 0))
    if anreise in ('auto', 'fahrrad'):
        fahr = min(km,20)*fahr_tage*0.30 + max(0,km-20)*fahr_tage*0.38
    elif anreise == 'oepnv':
        jobticket = form.get('jobticket','nein')
        fahr = 0 if jobticket == 'ja_frei' else float(form.get('oepnv_kosten',0))
    else:
        fahr = 0  # shuttle kostenlos, zu Fuß → keine Fahrtkosten
    reinig = arbeitstage * 1.60
    trink  = hotel_naechte * 3.60
    vma_72 = vma_72_tage * 14
    vma_73 = vma_73_tage * 14
    vma_74 = vma_74_tage * 28
    vma_in = vma_72 + vma_73 + vma_74

    # Brutto-Summe (= "Gesamtsumme der Aufwendungen" wie in FollowMe)
    gesamt = fahr + reinig + trink + vma_in + vma_aus

    # ── NETTO BERECHNEN ──────────────────────────────────────────
    # AG-Erstattungen abziehen:
    # - Z17 (Lohnsteuerbescheinigung): Fahrkostenzuschuss AG
    # - Z77 (Streckeneinsatz): Steuerfreie Spesen AG
    # Netto = direkt in WISO Reisenebenkosten eintragen!
    netto = gesamt - ag_z17 - z77

    # Build uploaded files summary for PDF
    uploaded_summary = []
    if files.get('lsb'):
        uploaded_summary.append(f"Lohnsteuerbescheinigung ({len(files['lsb'])} Datei(en))")
    if files.get('dp'):
        uploaded_summary.append(f"Flugstunden-Uebersichten ({len(files['dp'])} Datei(en))")
    if files.get('se'):
        uploaded_summary.append(f"Streckeneinsatz-Abrechnungen ({len(files['se'])} Datei(en))")
    not_uploaded = []
    if not files.get('lsb'): not_uploaded.append("Lohnsteuerbescheinigung")
    if not files.get('dp'):  not_uploaded.append("Flugstunden-Uebersichten (alle 12 Monate?)")
    if not files.get('se'):  not_uploaded.append("Streckeneinsatz-Abrechnungen (alle 12 Monate?)")

    return {
        'name':           form.get('name', 'Flugbegleiter'),
        'year':           form.get('year', 2025),
        'uploaded_summary': ', '.join(uploaded_summary),
        'not_uploaded':     ', '.join(not_uploaded) if not_uploaded else 'Alle Pflichtdokumente vorhanden',
        'datum':          datetime.now().strftime('%d.%m.%Y'),
        'km':             km,
        'arbeitstage':    arbeitstage,
        'fahr_tage':      fahr_tage,
        'hotel_naechte':  hotel_naechte,
        'vma_72_tage':    vma_72_tage,
        'vma_73_tage':    vma_73_tage,
        'vma_74_tage':    vma_74_tage,
        'vma_72':         vma_72,
        'vma_73':         vma_73,
        'vma_74':         vma_74,
        'vma_in':         vma_in,
        'vma_aus':        vma_aus,
        'fahr':           fahr,
        'reinig':         reinig,
        'trink':          trink,
        'gesamt':         gesamt,
        'ag_z17':         ag_z17,
        'spesen_gesamt':  spesen_gesamt,
        'spesen_steuer':  spesen_steuer,
        'z77':            z77,
        'netto':          netto,   # ← DAS trägt man in WISO ein
        'abrechnungen':   abrechnungen,
        'brutto':         lst['brutto'],
        'lohnsteuer':     lst['lohnsteuer'],
        'arbeitgeber':    lst.get('arbeitgeber','Deutsche Lufthansa AG'),
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
    # Dark premium palette matching website
    NAVY  = HexColor("#080c18")   # page background
    CARD  = HexColor("#0f1829")   # card background
    LIGHT = HexColor("#1a2744")   # lighter card
    GREEN = HexColor("#34d399")   # success green
    GREENL= HexColor("#0a2a1f")   # green tint
    RED   = HexColor("#f87171")   # error red
    REDL  = HexColor("#2a0f0f")   # red tint
    AMBER = HexColor("#fbbf24")   # gold/amber
    AMBERL= HexColor("#1f1800")   # amber tint
    GOLD  = HexColor("#fbbf24")
    GREY  = HexColor("#0d1526")   # subtle row
    GREYB = HexColor("#1e2d4a")   # border
    TEXT  = HexColor("#e2e8f0")   # main text
    TEXT2 = HexColor("#94a3b8")   # muted text
    BLUE  = HexColor("#60a5fa")   # accent blue
    WHITE = HexColor("#f1f5f9")   # near white

    base = getSampleStyleSheet()
    def ps(n,**kw): return ParagraphStyle(n,parent=base["Normal"],**kw)

    TH   = ps("th",  fontSize=9, textColor=WHITE, fontName="Helvetica-Bold",  leading=12)
    THR  = ps("thr", fontSize=9, textColor=WHITE, fontName="Helvetica-Bold",  leading=12, alignment=TA_RIGHT)
    TD   = ps("td",  fontSize=9, textColor=TEXT,  fontName="Helvetica",       leading=12)
    TDB  = ps("tdb", fontSize=9, textColor=TEXT,  fontName="Helvetica-Bold",  leading=12)
    TDR  = ps("tdr", fontSize=9, textColor=TEXT,  fontName="Helvetica",       leading=12, alignment=TA_RIGHT)
    TDRB = ps("tdrb",fontSize=9, textColor=TEXT,  fontName="Helvetica-Bold",  leading=12, alignment=TA_RIGHT)
    TRD  = ps("trd", fontSize=9, textColor=RED,   fontName="Helvetica",       leading=12, alignment=TA_RIGHT)
    TGN  = ps("tgn", fontSize=11,textColor=GREEN, fontName="Helvetica-Bold",  leading=14, alignment=TA_RIGHT)
    TGD  = ps("tgd", fontSize=9, textColor=GREEN, fontName="Helvetica-Bold",  leading=12, alignment=TA_RIGHT)
    TWISO= ps("twiso",fontSize=8,textColor=BLUE,  fontName="Helvetica-Bold",  leading=11, alignment=TA_RIGHT)
    SM   = ps("sm",  fontSize=8, textColor=TEXT2, fontName="Helvetica",       leading=11)
    NOTE = ps("note",fontSize=7.5,textColor=TEXT2,fontName="Helvetica",       leading=11)
    H1   = ps("h1",  fontSize=20,textColor=HexColor("#f1f5f9"),  fontName="Helvetica-Bold",  leading=24, spaceAfter=4)
    H2   = ps("h2",  fontSize=12,textColor=HexColor("#60a5fa"),  fontName="Helvetica-Bold",  leading=16, spaceBefore=14, spaceAfter=6)

    def eur(n):
        v=float(n)
        s=f"{abs(v):,.2f}".replace(",","X").replace(".",",").replace("X",".")
        return ("- " if v<0 else "")+s+" €"

    def on_page(canvas, doc):
        canvas.saveState()
        canvas.setFillColor(HexColor("#080c18"))
        canvas.rect(0, A4[1]-0.85*cm, A4[0], 0.85*cm, fill=1, stroke=0)
        # Gradient-like accent line
        canvas.setFillColor(HexColor("#f97316"))
        canvas.rect(0, A4[1]-0.85*cm, A4[0]*0.33, 0.03*cm, fill=1, stroke=0)
        canvas.setFillColor(HexColor("#ec4899"))
        canvas.rect(A4[0]*0.33, A4[1]-0.85*cm, A4[0]*0.33, 0.03*cm, fill=1, stroke=0)
        canvas.setFillColor(HexColor("#2563eb"))
        canvas.rect(A4[0]*0.66, A4[1]-0.85*cm, A4[0]*0.34, 0.03*cm, fill=1, stroke=0)
        canvas.setFillColor(HexColor("#e2e8f0"))
        canvas.setFont("Helvetica-Bold", 8)
        canvas.drawString(1.5*cm, A4[1]-0.58*cm, "AEROTAX — Werbungskosten-Auswertung 2025")
        canvas.setFont("Helvetica", 8)
        canvas.drawRightString(A4[0]-1.5*cm, A4[1]-0.5*cm, f"Seite {doc.page}  |  aerotax.de")
        canvas.setFillColor(HexColor("#080c18"))
        canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
        canvas.setFillColor(HexColor("#0d1526"))
        canvas.rect(0, 0, A4[0], 0.65*cm, fill=1, stroke=0)
        canvas.setFillColor(HexColor("#94a3b8"))
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(1.5*cm, 0.22*cm,
            "Alle Angaben ohne Gewähr. Bei steuerrechtlichen Fragen wenden Sie sich an einen Steuerberater.")
        canvas.restoreState()

    def tbl(rows, widths, header_bg=NAVY, total_row=-1, red_rows=None, green_rows=None):
        t = Table(rows, colWidths=widths)
        cmds = [
            ("BACKGROUND",    (0,0),(-1, 0), header_bg),
            ("ROWBACKGROUNDS",(0,1),(-1,-1), [WHITE,GREY]),
            ("TOPPADDING",    (0,0),(-1,-1), 8),
            ("BOTTOMPADDING", (0,0),(-1,-1), 8),
            ("LEFTPADDING",   (0,0),(-1,-1), 12),
            ("RIGHTPADDING",  (0,0),(-1,-1), 12),
            ("LINEBELOW",     (0,0),(-1,-1), 0.5, GREYB),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ]
        if total_row >= 0:
            cmds += [("BACKGROUND",(0,total_row),(-1,total_row), LIGHT),
                     ("LINEABOVE", (0,total_row),(-1,total_row), 1.5, NAVY)]
        for r in (red_rows or []):
            cmds.append(("BACKGROUND",(0,r),(-1,r), REDL))
        for r in (green_rows or []):
            cmds.append(("BACKGROUND",(0,r),(-1,r), GREENL))
        t.setStyle(TableStyle(cmds))
        return t

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
        leftMargin=1.8*cm, rightMargin=1.8*cm,
        topMargin=1.5*cm, bottomMargin=1.3*cm)
    S = []

    # ── DECKBLATT ────────────────────────────────────────────────
    S.append(Spacer(1, 0.5*cm))
    S.append(Paragraph("Steuerauswertung 2025", H1))
    S.append(Paragraph(
        f"{d['name']} — Anlage N / Werbungskosten · Deutsche Lufthansa AG",
        ps("sub", fontSize=10, textColor=TEXT2, fontName="Helvetica", leading=14)))
    S.append(HRFlowable(width="100%", thickness=1.5, color=HexColor("#1e2d4a"), spaceAfter=16))

    # ── SEKTION 1: AUFWENDUNGEN ───────────────────────────────────
    S.append(Paragraph("1. Errechnete Aufwendungen (aus Dienstplan-Übersichten)", H2))

    auf = [
        [Paragraph("Position", TH), Paragraph("Grundlage", TH), Paragraph("Betrag", THR)],
        [Paragraph(f"Fahrtkosten Homebase ({d['km']} km × {d['fahr_tage']} Tage, einfache Strecke)", TD),
         Paragraph(f"0,30 €/km bis 20 km + 0,38 €/km ab 20 km", SM),
         Paragraph(eur(d['fahr']), TDR)],
        [Paragraph(f"Reinigungskosten ({d['arbeitstage']} Arbeitstage × 1,60 €)", TD),
         Paragraph("Dienstkleidung pauschal", SM), Paragraph(eur(d['reinig']), TDR)],
        [Paragraph(f"Trinkgelder ({d['hotel_naechte']} Hotelnächte × 3,60 €)", TD),
         Paragraph("Reisenebenkosten pauschal", SM), Paragraph(eur(d['trink']), TDR)],
        [Paragraph(f"VMA Inland >8h ({d['vma_72_tage']} Tage × 14 €) — Zeile 72", TD),
         Paragraph("Eintagestouren ohne Übernachtung", SM), Paragraph(eur(d['vma_72']), TDR)],
        [Paragraph(f"VMA An-/Abreisetage ({d['vma_73_tage']} Tage × 14 €) — Zeile 73", TD),
         Paragraph("Mehrtäg. Einsätze mit Übernachtung", SM), Paragraph(eur(d['vma_73']), TDR)],
        [Paragraph(f"VMA 24h Inland ({d['vma_74_tage']} Tag × 28 €) — Zeile 74", TD),
         Paragraph("Volle Inlandstage", SM), Paragraph(eur(d['vma_74']), TDR)],
        [Paragraph("VMA Ausland — Zeile 76", TD),
         Paragraph("BMF-Pauschalen 2025 nach Land", SM), Paragraph(eur(d['vma_aus']), TDR)],
        [Paragraph("Gesamtsumme der Aufwendungen (Brutto)", TDB),
         Paragraph("", SM), Paragraph(eur(d['gesamt']), TDRB)],
    ]
    S.append(tbl(auf, [9.5*cm, 4*cm, 3.7*cm], total_row=8))

    # ── SEKTION 2: AG-ERSTATTUNGEN ────────────────────────────────
    S.append(Paragraph("2. Steuerfreie Erstattungen Lufthansa (Abzüge)", H2))

    se_rows = [
        [Paragraph("Abrechnung / Zeitraum", TH),
         Paragraph("Erstellt am", TH),
         Paragraph("Steuerpflichtig", THR),
         Paragraph("Steuerfrei (Z.77)", THR)],
    ]
    for a in d.get('abrechnungen',[]):
        se_rows.append([
            Paragraph(a.get('bezeichnung',''), TD),
            Paragraph(a.get('erstellt',''), TD),
            Paragraph(eur(a.get('steuerpflichtig',0)), ps("trd2",fontSize=9,textColor=RED,fontName="Helvetica",leading=12,alignment=TA_RIGHT)),
            Paragraph(eur(a.get('steuerfrei',0)), TGD),
        ])
    se_rows.append([
        Paragraph("Summe Streckeneinsatz-Abrechnungen", TDB),
        Paragraph("", TD),
        Paragraph(eur(d['spesen_steuer']), ps("trd3",fontSize=9,textColor=RED,fontName="Helvetica-Bold",leading=12,alignment=TA_RIGHT)),
        Paragraph(eur(d['z77']), TDRB),
    ])
    se_rows.append([
        Paragraph("+ AG-Fahrkostenzuschuss Zeile 17 (Lohnsteuerbescheinigung)", TD),
        Paragraph("", TD),
        Paragraph("", TDR),
        Paragraph(eur(d['ag_z17']),
                  ps("az17",fontSize=9,textColor=RED,fontName="Helvetica",leading=12,alignment=TA_RIGHT)),
    ])
    S.append(tbl(se_rows, [7.5*cm, 2.5*cm, 3.5*cm, 3.7*cm],
                 total_row=len(se_rows)-2))

    # ── SEKTION 3: ERGEBNIS UND WISO-ANLEITUNG ───────────────────
    S.append(Paragraph("3. Ergebnis — In WISO einzutragende Netto-Summe", H2))

    diff = [
        [Paragraph("Brutto-Aufwendungen gesamt", TD),
         Paragraph(eur(d['gesamt']), TDR)],
        [Paragraph("− AG-Fahrkostenzuschuss (Zeile 17)", TD),
         Paragraph("− "+eur(d['ag_z17']), TRD)],
        [Paragraph("− Steuerfreie Spesen Lufthansa (Zeile 77)", TD),
         Paragraph("− "+eur(d['z77']), TRD)],
        [Paragraph("= Einzutragender Betrag (Reisenebenkosten in WISO)", TDB),
         Paragraph(eur(d['netto']), TGN)],
    ]
    dt = Table(diff, colWidths=[13.5*cm, 3.7*cm])
    dt.setStyle(TableStyle([
        ("ROWBACKGROUNDS",(0,0),(-1,-2), [WHITE,GREY]),
        ("BACKGROUND",  (0,1),(-1, 2), REDL),
        ("BACKGROUND",  (0,-1),(-1,-1), GREENL),
        ("LINEBELOW",   (0,-1),(-1,-1), 2, GREEN),
        ("LINEABOVE",   (0,-1),(-1,-1), 1, GREYB),
        ("LINEBELOW",   (0,0),(-1,-2), 0.5, GREYB),
        ("TOPPADDING",  (0,0),(-1,-1), 10),
        ("BOTTOMPADDING",(0,0),(-1,-1), 10),
        ("LEFTPADDING", (0,0),(-1,-1), 12),
        ("RIGHTPADDING",(0,0),(-1,-1), 12),
    ]))
    S.append(dt)
    S.append(Spacer(1, 0.3*cm))

    # ── WISO-ANLEITUNG ────────────────────────────────────────────
    S.append(PageBreak())
    S.append(Paragraph("4. WISO-Anleitung — So trägst du den Betrag ein", H2))

    wiso_steps = [
        ("1", "WISO öffnen und navigieren",
         "Ausgaben → Werbungskosten → Reisekosten → Zusammengefasste Auswärtstätigkeiten → Neuer Eintrag",
         "", WHITE),
        ("2", "Beschreibung eingeben",
         'Im Feld "Beschreibung" eintragen:',
         '"Weitere Werbungskosten gemäss Dienstplanauswertung AeroTax"',
         WHITE),
        ("3", "Betrag unter Reisenebenkosten eintragen",
         'Bei "Fahrt- und Übernachtungskosten, Reisenebenkosten" → Reisenebenkosten eintragen:',
         eur(d['netto']),
         AMBERL),
        ("4", "AeroTax-PDF als Nachweis beilegen",
         'Da du auf die AeroTax-Auswertung verweist, dieses PDF dem Finanzamt einreichen. '
         'Bei WISO Steuer:Versand nach dem Einreichen direkt nachreichen.',
         "✓ Fertig",
         GREENL),
    ]

    for nr, titel, pfad, wert, bg in wiso_steps:
        val_color = GREEN if bg==GREENL else (AMBER if bg==AMBERL else TEXT)
        row = Table([[
            Paragraph(nr, ps(f"wn{nr}", fontSize=16, textColor=val_color,
                fontName="Helvetica-Bold", alignment=TA_RIGHT, leading=20)),
            Table([
                [Paragraph(pfad, ps(f"wp{nr}", fontSize=8, textColor=BLUE,
                    fontName="Helvetica-Bold", leading=11))],
                [Paragraph(titel, ps(f"wt{nr}", fontSize=10, textColor=TEXT,
                    fontName="Helvetica-Bold", leading=13))],
            ] + ([
                [Paragraph(wert, ps(f"wv{nr}", fontSize=14 if nr!='3' else 18,
                    textColor=val_color, fontName="Helvetica-Bold", leading=18))]
            ] if wert else []),
            colWidths=[14*cm], style=[
                ("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3),
                ("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),
            ]),
        ]], colWidths=[1.2*cm, 14*cm])
        row.setStyle(TableStyle([
            ("BACKGROUND",    (0,0),(-1,-1), bg),
            ("BOX",           (0,0),(-1,-1), 0.8, GREYB if bg==WHITE else
                              (GREEN if bg==GREENL else GOLD)),
            ("TOPPADDING",    (0,0),(-1,-1), 14),
            ("BOTTOMPADDING", (0,0),(-1,-1), 14),
            ("LEFTPADDING",   (0,0),(-1,-1), 14),
            ("RIGHTPADDING",  (0,0),(-1,-1), 14),
            ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ]))
        S.append(row)
        S.append(Spacer(1, 0.18*cm))

    S.append(Spacer(1, 0.4*cm))
    S.append(HRFlowable(width="100%", thickness=0.5, color=GREYB, spaceAfter=8))
    S.append(Paragraph(
        f"Erstellt mit AeroTax · aerotax.de · {d['datum']}",
        ps("foot", fontSize=8, textColor=TEXT2, fontName="Helvetica",
           alignment=TA_CENTER, leading=12)))

    # ── HOCHGELADENE DOKUMENTE SEITE ─────────────────────────────
    S.append(PageBreak())
    S.append(Spacer(1, 0.5*cm))

    doc_h = ps("dh", fontSize=14, textColor=NAVY, fontName="Helvetica-Bold",
                leading=18, spaceAfter=6)
    doc_b = ps("db", fontSize=10, textColor=TEXT2, fontName="Helvetica",
                leading=15, spaceAfter=4)
    doc_g = ps("dg", fontSize=10, textColor=GREEN, fontName="Helvetica-Bold",
                leading=14)
    doc_r = ps("dr", fontSize=10, textColor=RED,   fontName="Helvetica-Bold",
                leading=14)

    S.append(Paragraph("Dokumenten-Nachweis", doc_h))
    S.append(HRFlowable(width="100%", thickness=1, color=NAVY, spaceAfter=12))
    S.append(Paragraph(
        "Folgende Dokumente wurden vom Nutzer hochgeladen und zur Berechnung verwendet:",
        doc_b))
    S.append(Spacer(1, 0.2*cm))

    for item in d.get('uploaded_summary','').split(', '):
        if item.strip():
            S.append(Paragraph("✓ " + item.strip(), doc_g))

    not_up = d.get('not_uploaded','')
    if not_up and not_up != 'Alle Pflichtdokumente vorhanden':
        S.append(Spacer(1, 0.3*cm))
        S.append(Paragraph("Nicht hochgeladene Dokumente:", doc_b))
        for item in not_up.split(', '):
            if item.strip():
                S.append(Paragraph("✗ " + item.strip() + " — fehlend bei der Berechnung", doc_r))
        S.append(Spacer(1, 0.2*cm))
        S.append(Paragraph(
            "HINWEIS: Da nicht alle Dokumente hochgeladen wurden, "
            "kann AeroTax keine Garantie für die Vollständigkeit der Berechnung übernehmen. "
            "Der Nutzer wurde auf fehlende Dokumente hingewiesen.",
            ps("warn", fontSize=9, textColor=RED, fontName="Helvetica",
               leading=13, backColor=HexColor("#fee2e2"), borderPad=6)))
    else:
        S.append(Spacer(1, 0.2*cm))
        S.append(Paragraph("✓ Alle Pflichtdokumente wurden hochgeladen.", doc_g))

    # ── UNTERSCHRIFTSSEITE ───────────────────────────────────────
    S.append(PageBreak())
    S.append(Spacer(1, 1.5*cm))

    # Header
    sign_head = ps("sh", fontSize=14, textColor=NAVY,
                   fontName="Helvetica-Bold", leading=18, spaceAfter=6)
    sign_body = ps("sb", fontSize=10, textColor=TEXT2,
                   fontName="Helvetica", leading=15, spaceAfter=4)
    sign_small= ps("ss", fontSize=8.5, textColor=TEXT3,
                   fontName="Helvetica", leading=12)

    S.append(Paragraph("Bestätigung & Haftungsausschluss", sign_head))
    S.append(HRFlowable(width="100%", thickness=1, color=NAVY, spaceAfter=16))

    S.append(Paragraph(
        f"Die vorliegende Steuerauswertung für das Jahr {d.get('year',2025)} "
        f"wurde auf Basis der von <strong>{d['name']}</strong> hochgeladenen "
        f"Originaldokumente erstellt und durch den Eigentümer geprüft und bestätigt.",
        sign_body))
    S.append(Spacer(1, 0.3*cm))

    S.append(Paragraph(
        "<strong>Haftungsausschluss:</strong> Diese Auswertung wurde mit größter Sorgfalt "
        "erstellt und dient als Orientierungshilfe für die Steuererklärung. "
        "AeroTax (aerosteuer.de) übernimmt keine Haftung für steuerliche Nachforderungen, "
        "Bußgelder oder sonstige Schäden, die aus der Verwendung dieser Auswertung entstehen. "
        "Diese Auswertung ersetzt keine steuerrechtliche Beratung durch einen zugelassenen "
        "Steuerberater. Alle Angaben ohne Gewähr.",
        sign_body))

    S.append(Spacer(1, 1.2*cm))

    # Signature table
    from datetime import date
    heute = date.today().strftime("%d.%m.%Y")
    sig_tbl = Table([
        [
            Table([
                [Paragraph("Datum", sign_small)],
                [Paragraph(heute, ps("sd", fontSize=13, textColor=TEXT,
                    fontName="Helvetica-Bold", leading=16))],
            ], colWidths=[7*cm], style=[
                ("LINEABOVE",(0,0),(-1,0),1,GREYB),
                ("TOPPADDING",(0,0),(-1,-1),6),
                ("LEFTPADDING",(0,0),(-1,-1),0),
                ("RIGHTPADDING",(0,0),(-1,-1),0),
            ]),
            Spacer(1,1),
            Table([
                [Paragraph("Unterschrift " + d['name'], sign_small)],
                [Paragraph("", ps("se", fontSize=13, textColor=TEXT,
                    fontName="Helvetica-Bold", leading=16))],
            ], colWidths=[9*cm], style=[
                ("LINEABOVE",(0,0),(-1,0),1,NAVY),
                ("TOPPADDING",(0,0),(-1,-1),6),
                ("LEFTPADDING",(0,0),(-1,-1),0),
                ("RIGHTPADDING",(0,0),(-1,-1),0),
            ]),
        ]
    ], colWidths=[7*cm, 0.6*cm, 9*cm])
    sig_tbl.setStyle(TableStyle([
        ("VALIGN",(0,0),(-1,-1),"BOTTOM"),
        ("LEFTPADDING",(0,0),(-1,-1),0),
        ("RIGHTPADDING",(0,0),(-1,-1),0),
    ]))
    S.append(sig_tbl)
    S.append(Spacer(1, 0.8*cm))

    S.append(Paragraph(
        f"Erstellt mit AeroTax · aerosteuer.de · {d.get('datum', heute)}",
        ps("foot2", fontSize=8, textColor=TEXT3, fontName="Helvetica",
           alignment=1, leading=11)))

    doc.build(S, onFirstPage=on_page, onLaterPages=on_page)
    return buf.getvalue()


if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"AeroTax Backend startet auf Port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
