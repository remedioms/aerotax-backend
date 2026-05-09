# LEGAL — Compliance-Übersicht für AeroTax

**Stand:** 2026-05-09 · Version 5.3

Dieses Dokument fasst die rechtliche Aufstellung des Tools zusammen. Es ist Briefing für Anwalt, Steuerberater oder externen Reviewer — **kein** Ersatz für rechtliche Beratung.

---

## 1. Einordnung im StBerG

**§ 1 Steuerberatungsgesetz** beschränkt die geschäftsmäßige Hilfeleistung in Steuersachen auf befugte Personen (Steuerberater, Wirtschaftsprüfer, Rechtsanwälte etc.).

**Position von AeroTax:**
AeroTax ist ein **automatisiertes Berechnungs- und Dokumentationswerkzeug**. Es bietet **keine** Beratung im konkreten Einzelfall, sondern führt mathematische Operationen auf Basis der vom Nutzer hochgeladenen Daten durch und gibt das Ergebnis als strukturiertes PDF aus.

Konkrete Abgrenzungen:
- **Keine** individuelle steuerliche Beratung
- **Keine** Steuererklärung-Erstellung (User trägt selbst in WISO/ELSTER ein)
- **Keine** Vertretung gegenüber Finanzbehörden
- **Keine** Auslegung steuerrechtlicher Grenzfälle

In den AGB §2 ausdrücklich klargestellt: "AeroTAX erbringt keine geschäftsmäßige Hilfeleistung in Steuersachen iSv § 1 StBerG."

**Open:** Anwaltliche Bestätigung dass diese Abgrenzung trägt — empfohlene Konsultation eines Anwalts mit StBerG-Schwerpunkt vor breiter Markt-Skalierung.

---

## 2. AGB-Aufbau (§§ 1-10)

| § | Inhalt |
|---|---|
| 1 | Leistungsbeschreibung |
| 2 | Keine Hilfeleistung in Steuersachen iSv § 1 StBerG |
| 3 | Eigenprüfungspflicht des Nutzers |
| 4 | Pflichten des Nutzers / Missbrauch |
| 5 | Sofortleistung & Widerrufsrecht (§ 356 Abs. 5 BGB) |
| 6 | Preise |
| 7 | Haftungsbeschränkung |
| 8 | Keine Gewähr für Anerkennung durch Finanzamt |
| 9 | Online-Streitbeilegung (EU 524/2013) |
| 10 | Anwendbares Recht |

Ort: `~/Desktop/site/index.html`, Modal `modal-agb`.

---

## 3. DSGVO-Compliance

### Datenschutzerklärung (11 Sektionen)

| § | Inhalt |
|---|---|
| 1 | Verantwortlicher |
| 2 | Hochgeladene Dokumente (Zweckbindung + Löschung nach 60s) |
| 3 | Auswertungs-Ergebnis & 24h-Token |
| 4 | Auftragsverarbeiter (Cloudflare, Render, Supabase, Anthropic, Stripe) |
| 5 | Cookies & Tracking (keine) |
| 6 | Forum / Q&A |
| 7 | Welche Daten werden konkret verarbeitet (Datenkategorien) |
| 8 | Speicherdauer / Löschfristen (als Tabelle) |
| 9 | Technische und organisatorische Maßnahmen (Art. 32 DSGVO) |
| 10 | Auftragsverarbeitung nach Art. 28 DSGVO |
| 11 | Rechte der betroffenen Personen (Art. 15-21 DSGVO) |

Ort: `~/Desktop/site/index.html`, Modal `modal-datenschutz`.

### TOMs (Art. 32 DSGVO)

Im Code implementiert:
- TLS 1.2 / 1.3 (Cloudflare)
- AES-256 at-rest (Supabase Default)
- PII-Redaktion vor Disk-Persistierung (`_redact_pii` in `app.py`)
- IP-Rate-Limit (`_ip_rate_limited`)
- Auto-Cleanup alle 30 Min (`_cleanup_loop`)
- File-Purge 60s nach PDF-Erstellung
- Audit-Log-Auto-Löschung nach 48h

### Datenkategorien (mit Rechtsgrundlage)

| Kategorie | Beispiele | Rechtsgrundlage |
|---|---|---|
| Stammdaten | Name | Art. 6(1)(b) DSGVO Vertrag |
| Steuerl. Identifikatoren | Identnr, Personalnr, Geb-Datum | Art. 6(1)(b) DSGVO |
| Beschäftigungsdaten | Brutto, Lohnsteuer, SozVers | Art. 6(1)(b) DSGVO |
| Reisedaten | Routing, Hotelnächte | Art. 6(1)(b) DSGVO |
| Belege | Quittungen | Art. 6(1)(b) DSGVO |
| Zahlungsdaten | Stripe-Token | Art. 6(1)(b) DSGVO |
| Technische Daten | IP-Adresse | Art. 6(1)(f) DSGVO berechtigtes Interesse |

---

## 4. Auftragsverarbeiter (AVV-Status)

| Anbieter | Region | Zweck | AVV-Status |
|---|---|---|---|
| Cloudflare Pages | USA / EU-Edge | Frontend-Hosting | ⚠ User-Action: AVV im Account abrufbar |
| Render.com | USA | Backend-Server | ⚠ User-Action: AVV im Account abrufbar |
| Supabase | EU-Frankfurt | DB + File-Storage | ⚠ User-Action: AVV im Account abrufbar |
| Anthropic API | USA | KI-Analyse | ⚠ User-Action: AVV bei Account-Settings |
| Stripe | Irland (EU) | Zahlung | ⚠ User-Action: AVV im Stripe-Dashboard |

**Action für Eigentümer:** Alle 5 AVVs im Account abrufen, signieren, lokal archivieren. Siehe `~/Desktop/AeroTax_Action_Guide.pdf` Aufgabe 1.

---

## 5. Versicherungs-Status

| Versicherung | Status | Empfehlung |
|---|---|---|
| Berufshaftpflicht / Vermögensschadenhaftpflicht | ❌ nicht abgeschlossen | DRINGEND vor Markt-Launch (~25-50€/Mo) |
| Cyberversicherung | ❌ nicht abgeschlossen | EMPFOHLEN bei sensiblen Daten (~30-80€/Mo) |
| Produkthaftpflicht für Software | (in Berufshaftpflicht enthalten) | siehe oben |

**Action für Eigentümer:** siehe `~/Desktop/AeroTax_Action_Guide.pdf` Aufgaben 2.

---

## 6. Steuerberater-Review

**Status:** ❌ nicht durchgeführt

**Empfehlung:** Externer Steuerberater mit LH-Crew-Mandanten-Erfahrung sollte die Berechnungs-Logik absegnen, schriftliche Bestätigung dokumentiert ablegen. Siehe Action-Guide Aufgabe 3.

**Vorbereitungs-Material für den Termin:** `RECHENWEG.md` (15 Sektionen, fachliche Kurzreferenz mit Beispielrechnungen).

---

## 7. Marketing / Werbung — Risikoarme Formulierungen

### ✓ Zulässig
- "Berechnet deine Werbungskosten"
- "Aufstellung als Vorlage"
- "Vorbereitung für die Steuererklärung"
- "Werbungskosten-Übersicht"
- "Plausibilitäts-Hinweise"

### ❌ Vermeiden (zugesicherte Eigenschaften iSv § 309 Nr. 8 BGB / § 1 StBerG)
- "Wir optimieren deine Steuererklärung"
- "Maximale Erstattung"
- "Wir erstellen deine Steuererklärung"
- "Finanzamt-sicher"
- "Audit-sicher"
- "Wie ein Steuerberater"
- "100% korrekt"
- "Garantierte Anerkennung"

Status v5.2: Marketing-Texte gegen diese Liste geprüft. **Stand v5.3:** Hero/Tagline auf "Werbungskosten-Aufstellung" angepasst.

---

## 8. Open Items für rechtliche Vollständigkeit

**Anwaltliche Prüfung empfohlen:**
1. AGB-Text final durch Anwalt prüfen (besonders StBerG-Klausel und Haftungsbeschränkung)
2. Datenschutzerklärung von Datenschutzbeauftragten validieren
3. Impressum prüfen (TMG/MStV Pflichtangaben — aktuell auf der Seite vorhanden)

**Operativ noch offen:**
4. AVVs unterzeichnen (5 Anbieter)
5. Berufshaftpflicht abschließen
6. Cyberversicherung abschließen
7. Steuerberater-Review-Termin
8. Gewerbeanmeldung prüfen (Frankfurt — Ordnungsamt, falls noch nicht vorhanden)
9. Rechnungen über Stripe → ggf. ELSTER-Anmeldung als Kleinunternehmer / Ust-pflichtig

**Technisch offen:**
10. Sentry-Logging einrichten (für Compliance-Nachweis bei Vorfällen)
11. Backup-Strategie für Audit-Logs dokumentieren
12. Datenpannen-Meldekette (Art. 33 DSGVO) — wer informiert die Behörde innerhalb 72h?

---

## 9. Quellen / Referenzen

- **Steuerberatungsgesetz (StBerG):** § 1 (Beschränkung), § 5 (Geschäftsmäßigkeit), § 6 (Zulässige Tätigkeiten ohne Beratung)
- **DSGVO:** Art. 6, 13, 28, 32, 33
- **BGB:** § 309 Nr. 7+8 (unwirksame Haftungsausschluss-Klauseln), § 356 Abs. 5 (Widerrufsrecht digitale Inhalte)
- **EStG:** § 9 (Werbungskosten), § 9 Abs. 4a (VMA), § 3 Nr. 16 (steuerfreie Aufwendungen)
- **VSBG / EU 524/2013:** Online-Streitbeilegung
- **TMG / MStV:** Impressumspflicht
- **EU 965/2012 (EASA-FTL):** Flugzeit-/Layover-Regeln (Klassifikations-Logik in `referenz_faelle.txt`)

---

## 10. Versionshistorie zu rechtlichen Texten

- **v5.0** (2026-05-09): Disclaimer im PDF, AGB Modal mit Haftungsbeschränkung
- **v5.0.1**: Marketing-Phrasen entschärft (Hero, CTA, Animation)
- **v5.3**: AGB erweitert auf 10 §§ inkl. StBerG-Klausel + Eigenprüfungspflicht + Online-Streitbeilegung. Datenschutzerklärung erweitert auf 11 §§ inkl. TOMs + Löschfristen-Tabelle + Datenkategorien. PDF-Disclaimer um StBerG-Klausel ergänzt. LEGAL.md angelegt.
