# Final UI/Copy Release — Bericht

Stand: 2026-05-21. Status: **PASS TO CONTROLLED DEPLOY TEST**.

## 1. Hero-Copy (final)

- **Title**: `AeroTAX — Aus Dienstplan und Spesen wird dein Steuer-Überblick`
- **Headline (`.htag`)**: „Aus Dienstplan und Spesen wird dein Steuer-Überblick."
- **Subline (`.hsub`)**: „AeroTAX liest Dienstplan, Streckeneinsatz und Lohnsteuerdaten aus, gleicht steuerfreie Spesen ab und erstellt eine PDF-Auswertung — vorbereitet zum Eintragen."
- **Gradient-Claim**: „Dienstplan rein. Überblick raus.<br>Für Flugpersonal gemacht."
- **Nav-Tagline (`.ltag`)**: „Dienstplan rein. Überblick raus."

## 2. USP-Block (neu eingefügt, id="usp")

„Mehr als nur hochladen / AeroTAX zählt, gleicht ab und erklärt."

Fünf Glass-Cards:

1. **🧭 Dienstplan — Touren werden gelesen.** CAS/Dienstplan, Layover, Fahrtage, An- und Abreise, relevante Abwesenheiten — strukturiert ausgewertet.
2. **🔁 Streckeneinsatz — Spesen werden abgeglichen.** Streckeneinsatz-Abrechnungen gegen die Dienstplanlogik geprüft — Tag für Tag, Quelle gegen Quelle.
3. **💸 Steuerfrei — Z77 automatisch verrechnet.** Steuerfreie Spesen werden erkannt und im richtigen Topf gegengerechnet — kein Doppelansatz, kein Verlust.
4. **📄 PDF — Fertig zum Eintragen.** Beträge, Quellen, Hinweise — vorbereitet für WISO, ELSTER oder deine Steuersoftware. Mit Nachweis und Rechenweg.
5. **✈️ Flugpersonal — Crew-spezifisch.** Layover, An-/Abreise, Hotelnächte, Marker, Standby und Tour-Kontext — Logik für Flugpersonal, nicht generisch.

## 3. Trust-Badges (neu, 6 Stück direkt sichtbar)

- ✓ CAS / Dienstplan-Auswertung
- ✓ Streckeneinsatz-Abgleich
- ✓ Steuerfreie Spesen automatisch verrechnet
- ✓ PDF mit Quellen
- ✓ Keine Registrierung
- ✓ Dateien werden gelöscht

(Plus die bestehende Preis-Zeile darüber: „Einmalig 19,99 € · Kein Abo · PDF zum Download" — deckt „Einmalzahlung, kein Abo" ab.)

## 4. Download/Result-Copy (clean & positiv)

- **Rtag**: „Auswertung abgeschlossen" (unverändert)
- **rhd**: „Dein Steuer-Überblick ist fertig."
- **Subtext**: „Mit berechneten Werbungskosten, Spesen-Abgleich und Quellen deiner Werte — vorbereitet zum Eintragen."
- **PDF-Button**: „⬇ PDF herunterladen" (unverändert)
- **Keine** „keine Steuerberatung"-Zeile, **keine** Haftungsausschluss-Box, **kein** Warnsymbol im Download-Bereich.

## 5. Status / Progress / Review (clean & vertrauensvoll)

- `banner_title` im unknown-Fallback: „Wir lesen deine Unterlagen." (statt „Status wird geprüft")
- `banner_text`: „Einen Moment — wir prüfen den aktuellen Stand."
- Backend-State `needs_review` → „Auswertung vorbereitet — kurze Klärung nötig." (bereits vorhanden)
- Review-Item-Kopien (Backend) bleiben kontextspezifisch („7:55 Std. Abwesenheit", „14 € Verpflegungspauschale") — kein Markennamen-Spam, keine Angst-Sprache.

## 6. Source-Legende „Woher kommen die Werte?" (chip-style)

In der Detail-Tabelle am Ende:

> **Woher kommen die Werte?**
> [Dienstplan / CAS] [Streckeneinsatz] [Lohnsteuerdaten] [BMF-Pauschalen] [Deine Angabe *]
> Mit \* markierte Werte stammen aus deiner Eingabe oder Bestätigung. Pauschal-Ansatz bei Reinigung & Trinkgeld = Crew-typischer Erfahrungswert (keine BMF-Pauschale).

Keine technischen Strings wie `source_type=mixed` mehr.

## 7. Wo der rechtliche Hinweis steht — und warum noch safe

- **Footer**: „AeroTAX erstellt eine Aufstellung auf Basis deiner Unterlagen. Bitte prüfe die Angaben eigenverantwortlich oder mit deiner Steuerberatung." Klein, dezent, klar.
- **FAQ**: „Nein. AeroTAX ist keine Steuerberatung. AeroTAX erstellt eine strukturierte Auswertung und Dokumentation auf Basis deiner Unterlagen. Bei steuerlichen Zweifelsfragen wende dich bitte an eine Steuerberaterin, einen Steuerberater, einen Lohnsteuerhilfeverein oder deine Steuersoftware." (unverändert, in FAQ-Sektion).
- **Modals** (Impressum, Datenschutz, AGB & Haftung): vollständiger Haftungsausschluss + §3 StBerG bleibt drin (unverändert).
- **PDF-Ende**: bestehende „Wichtiger Hinweis"-Sektion bleibt im PDF erhalten.

**Rechtlich safe**:
- §3 StBerG-Distanzierung explizit in FAQ + AGB + Community-Footer.
- Footer enthält Empfehlung „eigenverantwortlich oder mit Steuerberatung" — Sorgfaltspflicht des Nutzers wird angesprochen.
- Modals enthalten vollständige Haftungsbeschränkung (§7 AGB).
- Keine Garantie-Claims, keine Prozentsätze, kein „Finanzamt-sicher".
- Die Streichung im Hero entfernt nur die *Sales-Negativ-Spirale*, nicht die rechtliche Substanz.

## 8. Entfernte / ersetzte Texte (Liste)

| Position | Vorher | Nachher |
|---|---|---|
| `<title>` Z.6 | „AeroTAX — Werbungskosten-Aufstellung für Lufthansa Flugpersonal" | „AeroTAX — Aus Dienstplan und Spesen wird dein Steuer-Überblick" |
| Nav `.ltag` Z.1587 | „Werbungskosten-Auswertung für Flugpersonal — keine Steuerberatung" | „Dienstplan rein. Überblick raus." |
| Status-Banner Z.1998 | `banner_title = 'Status wird geprüft'` | `'Wir lesen deine Unterlagen.'` |
| Hero `.htag` Z.2201 | „Die einfache Werbungskosten-Aufstellung für Flugpersonal" | „Aus Dienstplan und Spesen wird dein Steuer-Überblick." |
| Hero `.hsub` Z.2204 | „Lade deine Lufthansa-Dokumente hoch und erhalte eine übersichtliche Werbungskosten-Aufstellung für WISO, ELSTER oder deine Steuersoftware." | „AeroTAX liest Dienstplan, Streckeneinsatz und Lohnsteuerdaten aus, gleicht steuerfreie Spesen ab und erstellt eine PDF-Auswertung — vorbereitet zum Eintragen." |
| Hero-Slogan Z.2206 | „Drei Schritte. Weniger Recherche.<br>Mehr Überblick." | „Dienstplan rein. Überblick raus.<br>Für Flugpersonal gemacht." |
| Hero Badges Z.2214-2222 | „Keine Registrierung · WISO-kompatibel · Dateien werden gelöscht · Keine Installation" | „CAS / Dienstplan-Auswertung · Streckeneinsatz-Abgleich · Steuerfreie Spesen automatisch verrechnet · PDF mit Quellen · Keine Registrierung · Dateien werden gelöscht" |
| Hero Legal Z.2223 | „Klar strukturierte Vorbereitung — keine Steuerberatung." | **gelöscht** (in Footer verschoben) |
| USP-Block (neu) | — | 5 Cards mit CAS / SE / Z77 / PDF / Crew-Logik |
| Result rhd Z.2670 | „Werbungskosten-Auswertung" | „Dein Steuer-Überblick ist fertig." |
| Result subtext Z.2725 | „Zusammengefasste Werbungskosten-Auswertung für deine Steuererklärung." | „Mit berechneten Werbungskosten, Spesen-Abgleich und Quellen deiner Werte — vorbereitet zum Eintragen." |
| Source-Legende Z.4032-4044 | technische Liste mit „CAS = Dienstplan · SE = Streckeneinsatz · LSB = Lohnsteuerbescheinigung · BMF = §9 Abs. 4a EStG" | „Woher kommen die Werte?" mit 5 Chips |
| Footer Z.2848-2851 | „Werbungskosten-Aufstellung für Lufthansa-Flugpersonal · aerosteuer.de<br>Berechnungswerkzeug. Keine Steuerberatung." | „Für Flugpersonal gemacht · aerosteuer.de<br><sub>AeroTAX erstellt eine Aufstellung auf Basis deiner Unterlagen. Bitte prüfe die Angaben eigenverantwortlich oder mit deiner Steuerberatung.</sub>" |
| Community-Footer Z.8929 | „Community-Hilfe + automatische Orientierung · Ranking nutzt 30-Tage-Fenster · keine Steuerberatung iSd §3 StBerG" | „Community-Hilfe + Orientierung · Ranking nutzt 30-Tage-Fenster · Hinweise sind keine steuerliche Beratung iSd §3 StBerG" |
| Chat-Header-Sub Z.9315 | „Auswertung + WISO · keine Steuerberatung" | „Erklärt deinen Steuer-Überblick" |

**Stehen geblieben** (bewusst):
- 3× `// banner_title='Status wird geprüft'` in JS-Kommentaren (nur Bug-History, kein User-Output)
- 1× „keine Steuerberatung" in FAQ-Antwort (rechtlich notwendig, allowed location)
- 2× „Haftungsausschluss" als Modal-Überschriften (rechtlich notwendig)
- 4× „Haftung"-Vorkommen in AGB/Datenschutz-Modal (rechtlich notwendig)
- Plausi-Pattern „Warnung" in JS-Klassifikator (kein User-String, interne Klassifikation für gelbe Hinweise)
- Positive Copy „Keine unklaren Reisetage erkannt" (positiver Ergebnis-Hinweis)

## 9. Dateien geändert

- `~/Desktop/site/index.html` — 14 Edits (Title, Nav-Tagline, Status-Banner, Hero h1/sub/Slogan/Badges, Hero-Legal-Hint gelöscht, USP-Block eingefügt, Result-Header/Subtext, Footer, Community-Footer, Chat-Header, Source-Legende-Block)
- `tests/test_frontend_release_copy.mjs` — neu, 45 Static-Audit-Tests (Hero / USP / Trust / Legal-Placement / Status / Source-Legend / Download / Infra)
- `tests/test_source_breakdown_labeling.py` — `test_ui_contains_source_legend_text` an neue Chip-Sprache angepasst (alte Schlüssel waren technisch, neue user-friendly)

## 10. Tests + Ergebnis

| Suite | Vorher | Jetzt |
|---|---|---|
| `test_frontend_release_copy.mjs` (neu) | — | **45/45 grün** |
| `test_frontend_state_machine_live_run.mjs` | 28 | 28 grün |
| `test_frontend_scroll_helpers.mjs` | 15 | 15 grün |
| `test_frontend_progress_shimmer.mjs` | 23 | 23 grün |
| `test_source_breakdown_labeling.py` | 17 (1 wäre rot) | 17/17 grün (Test angepasst) |
| Full backend pytest | 2148 | **2148 passed, 13 skipped, 13 xfailed** |
| **TOTAL** | — | **2148 backend + 111 frontend = 2259 tests, 0 failed** |

Forbidden-String-Audit:
- „Werbungskosten-Auswertung für Flugpersonal": 1 → 0
- „Klar strukturierte Vorbereitung": 1 → 0
- „keine Steuerberatung" sichtbar im Hauptflow: 4 → 0 (1 verbleibend in FAQ, rechtlich gewollt)
- „Status wird geprüft" als User-Banner: 1 Assignment → 0 (3 verbleibend als JS-Kommentare)
- „Brutto-Aufwendungen gesamt": 0 (unverändert clean)
- „onrender": 0 / „RENDER_FALLBACK": 0

## 11. Frontend-Deploy nötig?

**Ja** — alle Änderungen sind in `~/Desktop/site/index.html`, das ist ein direct-upload Cloudflare Pages Projekt. Ohne `wrangler pages deploy` ist die alte Copy weiter live.

Befehl (wenn du grünes Licht gibst):

```
wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true
```

Backend ist **nicht** betroffen — keine `app.py`-Änderung in dieser Phase.

## 12. Recommendation

**PASS TO CONTROLLED DEPLOY TEST.**

Begründung:
- Hero/USP/Badges erfüllen die Brand-Richtung (premium, selbstbewusst, crew-spezifisch).
- USP-Block macht den Mehrwert klar (CAS + SE + Z77 + PDF + Crew-Logik).
- Source-Labels sind user-friendly (Chip-Style statt source_type=…).
- Rechtlicher Hinweis ist aus dem Sales-Flow raus, bleibt aber in Footer + FAQ + Modals + PDF-Ende erhalten — Sorgfaltspflicht-Vertretung intakt.
- Status-Banner spricht aktive Stimme („Wir lesen…" statt „Status wird geprüft").
- 45 neue Static-Tests gegen Copy-Regression.
- 2148 backend + 111 frontend = 2259 Tests, 0 Failures.

**Hard-Stops eingehalten:** Kein Deploy. Kein Live-Run. Kein Production-Switch. Stop nach Bericht.
