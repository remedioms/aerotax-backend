#!/usr/bin/env python3
"""Erzeugt eine PDF-Anleitung mit allen User-Actions die der Eigentümer
selbst erledigen muss (AVVs, Versicherung, Steuerberater-Review etc).

Output: ~/Desktop/AeroTax_Action_Guide.pdf
"""
import os
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                  PageBreak, HRFlowable, Table, TableStyle)
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT


# ── Palette (matched zum AeroTax-PDF) ──
BG       = HexColor('#060a16')
BG_DARK  = HexColor('#0a1224')
BG_CARD  = HexColor('#0f1830')
TEXT     = HexColor('#f1f5f9')
TEXT2    = HexColor('#94a3b8')
TEXT3    = HexColor('#4a5a72')
LINE     = HexColor('#1e3050')
LINE2    = HexColor('#2a3f5e')
GOLD     = HexColor('#fbbf24')
RED      = HexColor('#ef4444')
GREEN    = HexColor('#10b981')
BLUE     = HexColor('#60a5fa')


def make_pdf(output_path):
    base = getSampleStyleSheet()
    def ps(name, **kw):
        return ParagraphStyle(name, parent=base['Normal'], **kw)

    def page_bg(canv, doc):
        canv.saveState()
        canv.setFillColor(BG)
        canv.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
        # Footer
        canv.setFont('Helvetica', 7.5)
        canv.setFillColor(TEXT3)
        canv.drawString(2*cm, 1*cm, 'AeroTax — Action Guide für Miguel')
        canv.drawRightString(A4[0]-2*cm, 1*cm, f'Seite {doc.page}')
        canv.restoreState()

    doc = SimpleDocTemplate(output_path, pagesize=A4,
                            leftMargin=2.2*cm, rightMargin=2.2*cm,
                            topMargin=1.8*cm, bottomMargin=1.5*cm)
    S = []

    # ══════════════════════════════════════════════════════
    # COVER
    # ══════════════════════════════════════════════════════
    S.append(Spacer(1, 5*cm))
    S.append(Paragraph('AEROTAX',
        ps('cv0', fontSize=9, textColor=TEXT3, fontName='Helvetica-Bold',
           leading=12, alignment=TA_CENTER, spaceAfter=18, letterSpacing=3)))
    S.append(Paragraph('Action Guide',
        ps('cv1', fontSize=32, textColor=TEXT, fontName='Helvetica',
           leading=36, alignment=TA_CENTER, spaceAfter=12)))
    S.append(Paragraph('Was du selbst tun musst — und warum',
        ps('cv2', fontSize=13, textColor=TEXT2, fontName='Helvetica',
           leading=18, alignment=TA_CENTER, spaceAfter=30)))
    S.append(HRFlowable(width='10%', thickness=0.5, color=LINE2,
        hAlign='CENTER', spaceAfter=24))
    S.append(Paragraph(
        'Dein Tool ist technisch fertig und absichert. Aber es gibt 7 Punkte '
        'die du selbst erledigen musst — Vertragspapier, Versicherung, externe Reviews. '
        'Dieses Dokument erklärt jeden Punkt für sich: was es ist, warum du es brauchst, '
        'was du konkret tun musst, und was es kostet.',
        ps('cv3', fontSize=10.5, textColor=TEXT2, fontName='Helvetica',
           leading=18, alignment=TA_CENTER, spaceAfter=40)))
    S.append(Paragraph(f'Stand {datetime.now().strftime("%d.%m.%Y")}',
        ps('cv4', fontSize=8.5, textColor=TEXT3, fontName='Helvetica',
           leading=12, alignment=TA_CENTER, letterSpacing=1.5)))

    # ══════════════════════════════════════════════════════
    # PRIO-ÜBERSICHT
    # ══════════════════════════════════════════════════════
    S.append(PageBreak())
    S.append(Spacer(1, 0.5*cm))
    S.append(Paragraph('Reihenfolge nach Wichtigkeit',
        ps('h1', fontSize=20, textColor=TEXT, fontName='Helvetica',
           leading=24, alignment=TA_LEFT, spaceAfter=8)))
    S.append(Paragraph('Was zuerst erledigt werden sollte',
        ps('h1s', fontSize=10.5, textColor=TEXT2, fontName='Helvetica',
           leading=14, alignment=TA_LEFT, spaceAfter=20)))
    S.append(HRFlowable(width='100%', thickness=0.4, color=LINE, spaceAfter=18))

    prios = [
        ('Sehr hoch', '1.', 'AVVs unterzeichnen', 'kostenlos · 30 Min',
         'DSGVO-Bußgeld-Risiko, ohne kostet nichts'),
        ('Sehr hoch', '2.', 'Berufshaftpflicht', '~30-50€/Mo · 1h',
         'Schützt bei Klagen — Existenz-Risiko'),
        ('Hoch',      '3.', 'Steuerberater-Review', '~200-500€ · 1 Termin',
         'Vor Steuersaison, bevor User-Volumen wächst'),
        ('Mittel',    '4.', 'Sentry/Logging', 'kostenlos · 5 Min',
         'Wenn du wachsen oder profi werden willst'),
        ('Mittel',    '5.', 'Render Pro Plan', '$7/Mo · 1 Klick',
         'Erst wenn Server-Crashes auftreten'),
        ('Niedrig',   '6.', 'Korrektur-Frontend', 'Programmier-Zeit · später',
         'Nur wenn User es oft nachfragen'),
        ('Saisonal',  '7.', 'BMF-Sätze 2026 prüfen', '30 Min · Dezember 2026',
         'Jährlicher Reminder, im Code dokumentiert'),
    ]
    pdata = [['Prio', 'Nr', 'Aufgabe', 'Aufwand', 'Wann']]
    for p in prios:
        pdata.append(list(p))
    ptab = Table(pdata, colWidths=[2*cm, 0.9*cm, 4.6*cm, 3.8*cm, 5.3*cm])
    ptab.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), BG_CARD),
        ('TEXTCOLOR', (0,0), (-1,0), TEXT),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 8.5),
        ('TEXTCOLOR', (0,1), (-1,-1), TEXT2),
        ('FONTNAME', (0,1), (-1,-1), 'Helvetica'),
        ('FONTNAME', (2,1), (2,-1), 'Helvetica-Bold'),
        ('TEXTCOLOR', (2,1), (2,-1), TEXT),
        ('TEXTCOLOR', (0,1), (0,2), RED),
        ('TEXTCOLOR', (0,3), (0,3), GOLD),
        ('TEXTCOLOR', (0,4), (0,5), BLUE),
        ('TEXTCOLOR', (0,6), (0,6), TEXT3),
        ('TEXTCOLOR', (0,7), (0,7), TEXT3),
        ('FONTNAME', (0,1), (0,-1), 'Helvetica-Bold'),
        ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ('LEFTPADDING', (0,0), (-1,-1), 6),
        ('RIGHTPADDING', (0,0), (-1,-1), 6),
        ('TOPPADDING', (0,0), (-1,-1), 8),
        ('BOTTOMPADDING', (0,0), (-1,-1), 8),
        ('LINEBELOW', (0,0), (-1,0), 0.5, LINE2),
        ('LINEBELOW', (0,1), (-1,-1), 0.2, LINE),
    ]))
    S.append(ptab)
    S.append(Spacer(1, 0.8*cm))
    S.append(Paragraph(
        '<b>Diese Woche:</b> Punkt 1 (kostenlos, 30 Min) + Punkt 2 (Versicherung anfragen). '
        'Das sind die zwei Sachen die echtes Existenz-Risiko abdecken.',
        ps('prio_note', fontSize=9.5, textColor=GOLD, fontName='Helvetica',
           leading=14, alignment=TA_LEFT, leftIndent=6, rightIndent=6,
           spaceBefore=8)))

    # ══════════════════════════════════════════════════════
    # 0. WARUM DEIN DISCLAIMER NICHT REICHT
    # ══════════════════════════════════════════════════════
    S.append(PageBreak())
    S.append(Spacer(1, 0.4*cm))
    S.append(Paragraph('Vorab — warum reicht der Disclaimer nicht?',
        ps('q0h', fontSize=20, textColor=TEXT, fontName='Helvetica',
           leading=24, alignment=TA_LEFT, spaceAfter=8)))
    S.append(Paragraph('Du hast recht: User muss Werte selbst prüfen. Trotzdem brauchst du Versicherung. Hier warum.',
        ps('q0s', fontSize=10.5, textColor=TEXT2, fontName='Helvetica',
           leading=15, alignment=TA_LEFT, spaceAfter=18)))
    S.append(HRFlowable(width='100%', thickness=0.4, color=LINE, spaceAfter=14))

    arguments = [
        ('Deutsche Gerichte mögen Haftungsausschlüsse nicht',
         'Nach BGB § 309 Nr. 7 + Nr. 8 sind Haftungsausschlüsse in AGB unwirksam '
         'für grobe Fahrlässigkeit und zugesicherte Eigenschaften. Du kannst dich '
         'NICHT mit "alle Angaben ohne Gewähr" freistellen, wenn dein Tool einen '
         'offensichtlichen Bug hat.'),
        ('Deine Werbung wird gegen dich verwendet',
         'Auf deiner Seite steht "audit-sicher" und "FollowMe-Standard-konform". '
         'Das sind zugesicherte Eigenschaften. Wenn das Tool nicht so rechnet wie '
         'beworben → User sagt "ich vertraute dem Versprechen" → Disclaimer hilft nicht.'),
        ('User-Eigenverantwortung hilft, aber nicht immer',
         'User ist Laie, du bist der Experte (in seinen Augen). Dein Tool sagt '
         '"Z76 = 5.980€" — er muss sich darauf verlassen können dass das ungefähr '
         'stimmt. Sonst wäre das Tool sinnlos.'),
        ('Auch wenn du gewinnst — die Verteidigung kostet',
         'Selbst wenn am Ende abgewiesen wird: Anwalt 2.000-8.000€, Gerichtskosten '
         '5.000-30.000€, Monate Zeit, Stress, Reputation. Eine Versicherung zahlt '
         'das alles ab — auch bei berechtigtem Disclaimer.'),
        ('Realistisches Klage-Szenario',
         'Crew-Member trägt Werte in WISO ein → Finanzamt fordert 3.500€ nach + '
         '800€ Strafzinsen → User schreibt dir, will Schaden ersetzt → du sagst nein '
         '→ er klagt vor Amtsgericht (geht ohne Anwalt). Du brauchst Anwalt. Versicherung '
         'zahlt Anwalt + falls verloren auch den Schaden.'),
    ]
    for title, body in arguments:
        S.append(Paragraph(f'<b>{title}</b>',
            ps(f't{id(title)}', fontSize=11, textColor=TEXT, fontName='Helvetica-Bold',
               leading=15, alignment=TA_LEFT, spaceAfter=4)))
        S.append(Paragraph(body,
            ps(f'b{id(title)}', fontSize=9.5, textColor=TEXT2, fontName='Helvetica',
               leading=14, alignment=TA_LEFT, spaceAfter=12)))

    S.append(Spacer(1, 0.5*cm))
    S.append(HRFlowable(width='30%', thickness=0.4, color=LINE2,
        hAlign='LEFT', spaceAfter=10))
    S.append(Paragraph('Fazit',
        ps('q0fh', fontSize=11, textColor=TEXT, fontName='Helvetica-Bold',
           leading=15, alignment=TA_LEFT, spaceAfter=4, letterSpacing=1)))
    S.append(Paragraph(
        'Der Disclaimer schwächt dein Risiko ab — er macht es nicht null. Eine '
        'Berufshaftpflicht-Versicherung kostet 25-50€/Monat (1-2% deiner Marge) und '
        'schützt dich vor Verteidigungskosten + Schadenersatz. Standard-Geschäfts-Hygiene.',
        ps('q0f', fontSize=9.5, textColor=TEXT2, fontName='Helvetica',
           leading=15, alignment=TA_LEFT)))

    # ══════════════════════════════════════════════════════
    # Helper für jede Aufgabe
    # ══════════════════════════════════════════════════════
    def task_section(num, title, prio, was, warum, tun_steps, kosten, zeit, when=None):
        S.append(PageBreak())
        S.append(Spacer(1, 0.4*cm))
        # Prio-Badge
        prio_color = {'Sehr hoch': RED, 'Hoch': GOLD, 'Mittel': BLUE,
                      'Niedrig': TEXT3, 'Saisonal': TEXT3}.get(prio, TEXT3)
        S.append(Paragraph(f'<font color="#{prio_color.hexval()[2:]}">●</font>  '
                           f'AUFGABE {num} · Priorität: {prio}',
            ps(f's{num}p', fontSize=8.5, textColor=TEXT3, fontName='Helvetica-Bold',
               leading=12, alignment=TA_LEFT, spaceAfter=10, letterSpacing=1.8)))
        S.append(Paragraph(title,
            ps(f's{num}t', fontSize=22, textColor=TEXT, fontName='Helvetica',
               leading=28, alignment=TA_LEFT, spaceAfter=18)))
        S.append(HRFlowable(width='100%', thickness=0.4, color=LINE, spaceAfter=16))

        # Was?
        S.append(Paragraph('Was ist das?',
            ps(f's{num}wh', fontSize=10.5, textColor=BLUE, fontName='Helvetica-Bold',
               leading=14, alignment=TA_LEFT, spaceAfter=4, letterSpacing=0.5)))
        S.append(Paragraph(was,
            ps(f's{num}w', fontSize=10, textColor=TEXT2, fontName='Helvetica',
               leading=15, alignment=TA_LEFT, spaceAfter=14)))

        # Warum?
        S.append(Paragraph('Warum brauchst du das?',
            ps(f's{num}whyh', fontSize=10.5, textColor=GOLD, fontName='Helvetica-Bold',
               leading=14, alignment=TA_LEFT, spaceAfter=4, letterSpacing=0.5)))
        S.append(Paragraph(warum,
            ps(f's{num}why', fontSize=10, textColor=TEXT2, fontName='Helvetica',
               leading=15, alignment=TA_LEFT, spaceAfter=14)))

        # Was tun?
        S.append(Paragraph('Was musst du tun?',
            ps(f's{num}toh', fontSize=10.5, textColor=GREEN, fontName='Helvetica-Bold',
               leading=14, alignment=TA_LEFT, spaceAfter=6, letterSpacing=0.5)))
        for i, step in enumerate(tun_steps, 1):
            S.append(Paragraph(f'<b>{i}.</b> &nbsp; {step}',
                ps(f's{num}st{i}', fontSize=10, textColor=TEXT2, fontName='Helvetica',
                   leading=15, alignment=TA_LEFT, leftIndent=12, spaceAfter=4)))

        S.append(Spacer(1, 0.5*cm))

        # Kosten + Zeit
        kdata = [['Kosten', 'Zeitaufwand', 'Wann?'],
                 [kosten, zeit, when or 'Wenn dazu bereit']]
        ktab = Table(kdata, colWidths=[5.8*cm, 5.5*cm, 5.4*cm])
        ktab.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), BG_CARD),
            ('TEXTCOLOR', (0,0), (-1,0), TEXT3),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('FONTSIZE', (0,0), (-1,0), 7.5),
            ('FONTSIZE', (0,1), (-1,1), 9.5),
            ('TEXTCOLOR', (0,1), (-1,1), TEXT),
            ('FONTNAME', (0,1), (-1,1), 'Helvetica'),
            ('LEFTPADDING', (0,0), (-1,-1), 8),
            ('RIGHTPADDING', (0,0), (-1,-1), 8),
            ('TOPPADDING', (0,0), (-1,-1), 7),
            ('BOTTOMPADDING', (0,0), (-1,-1), 7),
            ('LINEBELOW', (0,0), (-1,0), 0.4, LINE2),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
        ]))
        S.append(ktab)

    # ══════════════════════════════════════════════════════
    # AUFGABE 1: AVVs
    # ══════════════════════════════════════════════════════
    task_section(
        num='1',
        title='AVVs (Auftragsverarbeitungs-Verträge) unterschreiben',
        prio='Sehr hoch',
        was=(
            'Verträge mit allen Diensten die du nutzt (Anthropic für KI, Stripe für '
            'Payment, Supabase für Datenbank, Render für Server). Darin steht: "Ihr '
            'dürft die User-Daten verarbeiten, müsst sie aber DSGVO-konform behandeln". '
            'Pflicht nach DSGVO Art. 28 wenn du externe Dienste mit Personendaten nutzt.'
        ),
        warum=(
            'Bei Datenschutz-Beschwerde oder Audit prüft die Behörde als erstes ob du '
            'AVVs hast. Ohne AVV → du verstößt gegen DSGVO → Bußgeld bis 20 Mio. € oder '
            '4% Jahresumsatz möglich. Realistisch: 5.000-50.000€ bei kleinerer Firma. '
            'Mit AVV = du bist auf der sicheren Seite, völlig kostenlos.'
        ),
        tun_steps=[
            'Bei <b>Anthropic</b> einloggen → console.anthropic.com → Settings → Privacy → "Data Processing Agreement" → akzeptieren.',
            'Bei <b>Stripe</b> einloggen → dashboard.stripe.com → Settings → Compliance → "Data Processing Agreement" → unterzeichnen.',
            'Bei <b>Supabase</b> einloggen → app.supabase.com → Account → Legal → "DPA" → akzeptieren.',
            'Bei <b>Render</b> einloggen → render.com → Account → Compliance → "DPA" prüfen (ist meist im Standard-Vertrag enthalten).',
            'PDFs aller AVVs in einem Ordner speichern (z.B. ~/Documents/AeroTax/AVVs/) — bei Behörden-Anfrage auf einen Klick verfügbar.',
        ],
        kosten='Kostenlos',
        zeit='~30 Minuten gesamt',
        when='Diese Woche — keine Ausrede',
    )

    # ══════════════════════════════════════════════════════
    # AUFGABE 2: Berufshaftpflicht
    # ══════════════════════════════════════════════════════
    task_section(
        num='2',
        title='Berufshaftpflicht-Versicherung',
        prio='Sehr hoch',
        was=(
            'Eine Versicherung die zahlt wenn jemand dich wegen deines Tools verklagt — '
            'sei es weil das Tool falsch gerechnet hat oder ein Datenschutz-Vorfall passiert ist. '
            'Deckt Anwaltskosten, Gerichtskosten und falls verloren auch den Schadenersatz.'
        ),
        warum=(
            'Dein Disclaimer auf der Website schützt dich nur teilweise (siehe Vorab-Seite). '
            'Selbst bei berechtigter Verteidigung kostet ein Gerichts-Verfahren 5.000-30.000€. '
            'Ohne Versicherung zahlst du das privat — bei größerer Klage existenzbedrohend. '
            'Mit Versicherung: 25-50€/Monat als planbare Geschäftsausgabe.'
        ),
        tun_steps=[
            'Anbieter vergleichen: <b>Hiscox</b>, <b>Exali</b>, <b>Hannoversche IT-Versicherung</b>, <b>Markel</b> haben Tarife für Solo-IT-Anbieter.',
            'Such-Stichworte: "Berufshaftpflicht IT-Dienstleister" oder "Vermögensschaden-Haftpflicht für Software-Anbieter".',
            'Online-Vergleichsportal nutzen (z.B. exali.de hat einen 5-Min-Konfigurator).',
            'Angabe: Solo-Selbstständig, Online-Tool, Steuer-/Finanz-Bereich, Umsatz <50k€/Jahr.',
            'Deckungssumme 250.000-500.000€ reicht typisch — höher = teurer.',
            'Police unterschreiben und in deinen Unterlagen ablegen.',
        ],
        kosten='~25-50€/Monat (300-600€/Jahr)',
        zeit='1-2 Stunden Recherche + Anmeldung',
        when='Diese oder nächste Woche',
    )

    # ══════════════════════════════════════════════════════
    # AUFGABE 3: Steuerberater-Review
    # ══════════════════════════════════════════════════════
    task_section(
        num='3',
        title='Steuerberater-Review',
        prio='Hoch',
        was=(
            'Ein zugelassener Steuerberater prüft die Berechnungs-Logik deines Tools auf '
            'fachliche Korrektheit. Du bekommst eine schriftliche Bestätigung dass das Tool '
            'aktuelle BMF-/EStG-konforme Werte ausspuckt.'
        ),
        warum=(
            'Du bist kein Steuerberater. Wenn das Tool falsch rechnet und ein User klagt, '
            'kannst du sagen: "Ich habe einen Steuerberater zur Prüfung eingesetzt, hier ist '
            'die Bestätigung." Das ist juristisch viel stärker als "ich hab gegoogelt". '
            'Außerdem deckt es Bugs auf die du selbst nicht siehst — Crew-spezifische Edge-Cases.'
        ),
        tun_steps=[
            'Steuerberater suchen der bereits Lufthansa-Crew-Mandanten hat (z.B. über die VC-Vereinigung Cockpit oder Crew-Foren erfragen).',
            'Termin vereinbaren — sag ihm dass du ein Online-Tool für Crew-Werbungskosten gebaut hast und Beratung zur Berechnung möchtest.',
            'Bei dem Termin: 2-3 anonymisierte Beispiel-Fälle (LSB + SE + DP) + dazu deine berechnete Auswertung mitnehmen.',
            'Frage konkret: "Stimmen Pendlerpauschale, Z72/Z73/Z76-Klassifikation und BMF-Auslandspauschalen?"',
            'Wenn er sagt "stimmt": frag nach einer schriftlichen Bestätigung (E-Mail reicht — "habe AeroTax-Berechnungs-Logik geprüft, ist BMF-/EStG-konform").',
            'Diese E-Mail aufheben — bei Klage Gold wert.',
        ],
        kosten='~200-500€ (Beratungs-Honorar)',
        zeit='1 Termin (~1h) + Vorbereitung',
        when='Vor März 2026 (Steuersaison startet April)',
    )

    # ══════════════════════════════════════════════════════
    # AUFGABE 4: Sentry/Logging
    # ══════════════════════════════════════════════════════
    task_section(
        num='4',
        title='Sentry / Error-Logging einrichten',
        prio='Mittel',
        was=(
            'Sentry ist ein Dienst der dich automatisch per E-Mail benachrichtigt wenn auf '
            'deiner Website ein Fehler auftritt — egal ob Backend-Crash oder Frontend-Bug. '
            'Du siehst sofort: was ist passiert, bei welchem User, in welcher Code-Zeile.'
        ),
        warum=(
            'Aktuell merkst du Bugs erst wenn ein User sich beschwert. Das ist zu spät — der '
            'Ärger ist schon da. Mit Sentry siehst du Fehler in Echtzeit, kannst sie fixen '
            'bevor der nächste User sie trifft. Bei Steuersaison mit vielen Usern unverzichtbar.'
        ),
        tun_steps=[
            'Auf <b>sentry.io</b> registrieren — kostenlos für bis zu 5.000 Events/Monat.',
            'Neues Projekt anlegen → "Python" / "Flask" auswählen.',
            'Du bekommst einen "DSN" (sieht aus wie https://abc123@sentry.io/12345).',
            'DSN an mich weiterleiten — ich integriere den Sentry-Client in app.py (5 Zeilen Code).',
            'Optional: zusätzlich Frontend-Sentry für Browser-Fehler einbauen.',
        ],
        kosten='Kostenlos (bis 5.000 Events/Monat)',
        zeit='5 Min Anmeldung + Integration durch mich',
        when='Wenn du dein Tool ernst meinst',
    )

    # ══════════════════════════════════════════════════════
    # AUFGABE 5: Render Pro
    # ══════════════════════════════════════════════════════
    task_section(
        num='5',
        title='Render auf Pro-Plan upgraden',
        prio='Mittel',
        was=(
            'Aktuell läuft dein Backend auf Render-Free mit 512 MB RAM. Bei der Steuersaison '
            'oder bei mehreren parallel laufenden Auswertungen reicht das nicht. Server crasht '
            'mit "Out of Memory" und User verliert seinen Job.'
        ),
        warum=(
            'Pro-Plan hat 2 GB RAM (4× mehr) und kann mehrere Auswertungen parallel verarbeiten. '
            'Außerdem: kein Cold-Start (auf Free schläft der Server nach 15 Min ein → erster '
            'User wartet 30 Sek). Pro = sofort verfügbar.'
        ),
        tun_steps=[
            'Auf <b>render.com</b> einloggen.',
            'Den AeroTax-Service auswählen.',
            'Tab "Settings" → "Instance Type" → ändere auf "Standard" (oder "Pro").',
            'Bezahlung läuft automatisch über deine hinterlegte Karte.',
            'Server startet neu (~3 Min).',
        ],
        kosten='$7/Monat für "Standard" (~6,50€). $25/Mo für "Pro".',
        zeit='1 Klick',
        when='Wenn Crashes auftreten oder vor Steuersaison',
    )

    # ══════════════════════════════════════════════════════
    # AUFGABE 6: Korrektur-Frontend
    # ══════════════════════════════════════════════════════
    task_section(
        num='6',
        title='Korrektur-Frontend für User',
        prio='Niedrig',
        was=(
            'Eine UI-Möglichkeit für User, das Ergebnis manuell zu korrigieren. Z.B. wenn '
            'die KI Z73 = 0 ausgibt aber der User merkt "ich hatte 2 Schulungen mit Hotel". '
            'User klickt "korrigieren", trägt 2 ein, PDF wird neu generiert.'
        ),
        warum=(
            'Macht das Tool besser für unsichere User. Reduziert deine Support-Anfragen ("die '
            'Auswertung stimmt nicht" → User kann selbst fixen). Aber: nicht dringend wenn die '
            'KI-Genauigkeit hoch ist. Erst implementieren wenn User es nachfragen.'
        ),
        tun_steps=[
            'Erstmal abwarten — wenn 5+ User korrektur-anfragen schicken: implementieren.',
            'Mir sagen "bau das" — ich brauche ~2-3 Tage.',
            'Frontend: Edit-Form mit allen Werten (Z72/Z73/Z74-Tage, fahr_tage, etc.).',
            'Backend: Endpoint /api/recompute/{token} der mit den korrigierten Werten ein neues PDF generiert.',
            'Audit-Trail: speichern was der User selbst geändert hat (für Steuerberater-Bestätigung).',
        ],
        kosten='Programmier-Zeit (meine), keine Geldkosten',
        zeit='2-3 Arbeitstage (meine)',
        when='Wenn 5+ User es nachfragen',
    )

    # ══════════════════════════════════════════════════════
    # AUFGABE 7: BMF 2026 Update
    # ══════════════════════════════════════════════════════
    task_section(
        num='7',
        title='BMF-Pauschalen für 2026 prüfen',
        prio='Saisonal',
        was=(
            'Das Bundesfinanzministerium veröffentlicht jedes Jahr neue Auslandsspesen-'
            'Pauschalen (Tagessätze pro Land). Z.B. "Indien Tagessatz 30€" könnte 2026 zu '
            '"32€" werden. Aktuell hardcoded im Code (Datei bmf_data.py).'
        ),
        warum=(
            'Wenn du 2027 für Steuerjahr 2026 noch die alten 2025er-Werte nutzt, sind die '
            'Auswertungen falsch. Steuerberater erkennen das sofort, User vertraut dir nicht mehr.'
        ),
        tun_steps=[
            'Im <b>Dezember 2026</b> (oder Januar 2027): Reminder im Code-Block erinnert dich.',
            'Google: "BMF-Schreiben Reisekosten 2027" oder "BMF Auslandstagegelder 2027".',
            'PDF vom Bundesfinanzministerium runterladen.',
            'Die neuen Tagessätze pro Land aus dem PDF extrahieren.',
            'Mir die PDF-Datei + neuen Werte schicken — ich update bmf_data.py.',
            'Oder: selbst machen — die Datei ist nur eine Tabelle mit Land → Werte.',
            'Im Code-Block "LAST-REVIEWED" und "NEXT-REVIEW"-Datum aktualisieren.',
        ],
        kosten='Kostenlos',
        zeit='30 Min einmal jährlich',
        when='Dezember 2026 / Januar 2027',
    )

    # ══════════════════════════════════════════════════════
    # CHECKLIST AM ENDE
    # ══════════════════════════════════════════════════════
    S.append(PageBreak())
    S.append(Spacer(1, 0.4*cm))
    S.append(Paragraph('Deine Checkliste',
        ps('chk_h', fontSize=20, textColor=TEXT, fontName='Helvetica',
           leading=24, alignment=TA_LEFT, spaceAfter=8)))
    S.append(Paragraph('Hak ab was du erledigt hast',
        ps('chk_s', fontSize=10.5, textColor=TEXT2, fontName='Helvetica',
           leading=14, alignment=TA_LEFT, spaceAfter=20)))
    S.append(HRFlowable(width='100%', thickness=0.4, color=LINE, spaceAfter=18))

    checks = [
        ('Diese Woche', [
            'AVV bei Anthropic akzeptiert',
            'AVV bei Stripe akzeptiert',
            'AVV bei Supabase akzeptiert',
            'AVV bei Render geprüft',
            'AVV-PDFs in Ordner abgelegt',
            'Berufshaftpflicht-Versicherung angefragt (mind. 2 Anbieter)',
        ]),
        ('Diesen Monat', [
            'Versicherungs-Police unterschrieben',
            'Steuerberater kontaktiert (LH-Crew-Erfahrung)',
            'Termin vereinbart',
        ]),
        ('Nächste 3 Monate', [
            'Steuerberater-Review-Termin durchgeführt',
            'Schriftliche Bestätigung erhalten + abgelegt',
            'Sentry-Account angelegt + DSN integriert',
        ]),
        ('Bei Bedarf', [
            'Render auf Pro upgegradet (wenn Crashes auftreten)',
            'Korrektur-Frontend besprochen (wenn User nachfragen)',
        ]),
        ('Dezember 2026', [
            'BMF-Schreiben 2027 runtergeladen',
            'Auslandsspesen-Tabelle aktualisiert',
            'LAST-REVIEWED-Datum im Code aktualisiert',
        ]),
    ]
    for grp_name, items in checks:
        S.append(Paragraph(grp_name,
            ps(f'chg_{id(grp_name)}', fontSize=11, textColor=GOLD,
               fontName='Helvetica-Bold', leading=14, alignment=TA_LEFT,
               spaceAfter=6, letterSpacing=1)))
        for it in items:
            S.append(Paragraph(f'☐ &nbsp; {it}',
                ps(f'ci_{id(it)}', fontSize=10, textColor=TEXT2,
                   fontName='Helvetica', leading=15, alignment=TA_LEFT,
                   leftIndent=14, spaceAfter=3)))
        S.append(Spacer(1, 0.3*cm))

    # ENDE
    S.append(Spacer(1, 1*cm))
    S.append(HRFlowable(width='30%', thickness=0.4, color=LINE2,
        hAlign='CENTER', spaceAfter=12))
    S.append(Paragraph('Das war alles. Nicht zaudern, anfangen.',
        ps('end', fontSize=11, textColor=TEXT, fontName='Helvetica-Bold',
           leading=16, alignment=TA_CENTER, letterSpacing=0.5)))
    S.append(Spacer(1, 0.4*cm))
    S.append(Paragraph(
        'Bei Fragen oder wenn ich etwas integrieren soll (Sentry, Korrektur-Frontend, '
        'BMF-Update) — einfach melden.',
        ps('end2', fontSize=9, textColor=TEXT3, fontName='Helvetica',
           leading=14, alignment=TA_CENTER)))

    doc.build(S, onFirstPage=page_bg, onLaterPages=page_bg)
    print(f'PDF generiert: {output_path}')


if __name__ == '__main__':
    output = os.path.expanduser('~/Desktop/AeroTax_Action_Guide.pdf')
    make_pdf(output)
