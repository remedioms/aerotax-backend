# AUDIT_BROWSER_QA — manuelle QA-Szenarien mit Checkboxen

> Jedes Szenario muss vor Beta-Launch grün durchgespielt werden.
> Status nur ✅ nach erfolgreichem Browser-Test mit Screenshot oder Trace.

---

## R) Recall (Code prüfen)

### R.1 — Falscher Code
- [ ] Token-Modal öffnen
- [ ] Eingabe: `AT-NOTEXIST123`
- [ ] „Code prüfen" klicken
- [ ] **Erwartet:** binnen 5s sichtbarer Fehler-Banner „❌ Code nicht gefunden oder abgelaufen"
- [ ] **Erwartet:** Button wieder aktiv mit Text „Code prüfen"
- [ ] **Erwartet:** Modal bleibt offen, User kann erneut eingeben
- [ ] **Verboten:** „Load failed", „undefined", „null", Stacktrace

### R.2 — Format-Fehler (kein AT- Prefix)
- [ ] Eingabe: `12345`
- [ ] **Erwartet:** sichtbar „❌ Code muss mit „AT-" beginnen"

### R.3 — Gültiger needs_review-Code
- [ ] Eingabe: `AT-89080734B3FDC191` (Mini-Run-Token von 2026-05-12)
- [ ] **Erwartet:** binnen 10s schließt Modal
- [ ] **Erwartet:** Result-Panel öffnet
- [ ] **Erwartet:** Banner „Auswertung vorbereitet — kurze Klärung nötig"
- [ ] **Erwartet:** KEIN „PDF herunterladen"-Button
- [ ] **Erwartet:** `pdf-locked-indicator` zeigt Lock-Reason
- [ ] **Erwartet:** Chat zeigt offene Punkte zur Klärung
- [ ] **Verboten:** finaler Betrag in groß als „abschließend" wirkend

### R.4 — Gültiger done-Code (nach Klärung)
- [ ] Erst Review-Punkte im Chat klären
- [ ] Token erneut prüfen
- [ ] **Erwartet:** Banner „Auswertung fertig"
- [ ] **Erwartet:** „⬇ PDF herunterladen" sichtbar + aktiv
- [ ] **Erwartet:** Finaler Betrag groß sichtbar
- [ ] PDF-Button klicken → PDF lädt herunter

### R.5 — Processing-Code (Job läuft noch)
- [ ] (Setup: aktiver Job-Token)
- [ ] **Erwartet:** Progress-Panel mit Live-Texten öffnet
- [ ] **Erwartet:** Code prominent sichtbar
- [ ] **Erwartet:** KEIN finaler Betrag

### R.6 — failed_support Token
- [ ] (Setup: Token von Job mit ALIGN_FAILED)
- [ ] **Erwartet:** Banner „Auswertung konnte nicht sicher abgeschlossen werden"
- [ ] **Erwartet:** Support-Button prominent
- [ ] **Erwartet:** KEIN Retry-Button
- [ ] **Erwartet:** KEIN PDF-Button

### R.7 — Network-Timeout
- [ ] DevTools → Network → Offline simulieren
- [ ] Code eingeben + klicken
- [ ] **Erwartet:** binnen 15s sichtbar „⚠️ Verbindung kurz unterbrochen"
- [ ] **Erwartet:** Button wieder aktiv

### R.8 — Button-Stuck-Prevention
- [ ] In jeder Situation (auch Crash): Button reaktiviert sich innerhalb 15s
- [ ] **Verboten:** Button dauerhaft disabled mit „⏳ Code wird geprüft…"

---

## P) PDF-Sichtbarkeit

### P.1 — needs_review zeigt KEIN PDF
- [ ] Recall mit needs_review-Token
- [ ] **Erwartet:** Im Result-Panel keine PDF-Buttons (header + main)
- [ ] **Erwartet:** Lock-Indikator-Element sichtbar
- [ ] **Verboten:** Auch verstecktes oder disabled-PDF-Button

### P.2 — done zeigt PDF
- [ ] Recall mit done-Token
- [ ] **Erwartet:** „⬇ PDF herunterladen" sichtbar
- [ ] Klick → PDF-Download startet

### P.3 — failed_* zeigt KEIN PDF
- [ ] Recall mit failed_retryable/failed_support
- [ ] **Erwartet:** KEIN PDF-Button, KEIN finaler Betrag

### P.4 — fetch_error zeigt KEIN PDF
- [ ] Network unterbrechen während Result-Page offen
- [ ] **Erwartet:** PDF wird ausgeblendet, friendly Fehler

### P.5 — Demo-PDF nur in Demo-Mode
- [ ] „Demo ansehen"-Button auf Landing
- [ ] **Erwartet:** Max Mustermann-Demo öffnet
- [ ] **Erwartet:** Klar als „DEMO" markiert
- [ ] Klick PDF in Demo → Demo-PDF
- [ ] Bei echtem Job: Demo-PDF darf NICHT fallback sein

---

## F) Forum (Q&A)

### F.1 — Forum lädt schnell
- [ ] Nav-Link „Forum" klicken
- [ ] **Erwartet:** Fragen-Liste binnen 2s sichtbar
- [ ] **Verboten:** Spinner > 5s

### F.2 — Frage stellen
- [ ] „Frage stellen" + Text + Submit
- [ ] **Erwartet:** binnen 3s erscheint Frage in Liste

### F.3 — Like / Unlike
- [ ] Like-Button auf Frage klicken
- [ ] **Erwartet:** Counter erhöht +1, Button-Highlight
- [ ] Erneut klicken (unlike)
- [ ] **Erwartet:** Counter -1

### F.4 — Comment
- [ ] Auf Frage klicken → Comment-Form
- [ ] Text + Submit
- [ ] **Erwartet:** Comment erscheint

### F.5 — Mobile Forum
- [ ] iPhone Safari: Forum öffnen
- [ ] Scroll funktioniert smooth
- [ ] Like-Tap-Area groß genug

### F.6 — Forum während Worker läuft
- [ ] Mini-Run starten
- [ ] Parallel Forum öffnen
- [ ] **Erwartet:** Forum lädt trotzdem <2s
- [ ] **Akzeptanz BUG-002**

---

## C) Chat

### C.1 — Chat bei done erklärt Berechnung
- [ ] Result-Page done
- [ ] Chat-Input „Wie kommt mein Z76 zustande?"
- [ ] **Erwartet:** Sonnet erklärt mit konkreten Zahlen

### C.2 — Chat bei processing blockt
- [ ] Job läuft
- [ ] Chat „Was ist mein Endbetrag?"
- [ ] **Erwartet:** „Deine Auswertung läuft noch…" (state-gate)
- [ ] **Verboten:** Sonnet halluziniert Betrag

### C.3 — Chat bei needs_review priorisiert Klärung
- [ ] Result needs_review, 3 pending Items
- [ ] Chat „Wie geht's weiter?"
- [ ] **Erwartet:** Chat führt durch offene Punkte

### C.4 — Chat-Reset löscht History
- [ ] Reset-Button klicken
- [ ] **Erwartet:** Chat-Bubble leer + Confirm-Modal

### C.5 — Mobile Chat
- [ ] Tastatur erscheint, Input bleibt sichtbar
- [ ] Send-Button reachable

---

## U) Upload / Payment

### U.1 — Upload aller 3 Pflicht-Files
- [ ] LSB, SE, CAS hochladen
- [ ] **Erwartet:** Status-Pille „✓ Alle 3 hochgeladen"
- [ ] „Weiter"-Button aktiv

### U.2 — Promo-Code SMOKETEST
- [ ] Promo-Code-Feld + `SMOKETEST` + Apply
- [ ] **Erwartet:** „Promo akzeptiert" + Stripe-Form versteckt

### U.3 — Stripe-Bezahlung
- [ ] Stripe-Form ausfüllen
- [ ] Test-Karte: 4242 4242 4242 4242
- [ ] **Erwartet:** Payment success → Process startet

### U.4 — Reload während Upload-Step
- [ ] Files hochladen, Reload
- [ ] **Erwartet:** Files aus localStorage restored ODER klarer Re-Upload-Hinweis

---

## A) Architektur (Performance unter Last)

### A.1 — Health unter Last
- [ ] Worker-Job starten (Tibor)
- [ ] Parallel `/api/health` 10× pollen
- [ ] **Erwartet:** alle <2s
- [ ] **Verboten:** 429 oder >5s

### A.2 — Session-Recall unter Last
- [ ] Worker-Job starten
- [ ] Parallel Recall mit Token
- [ ] **Erwartet:** Result öffnet <3s

### A.3 — Forum unter Last
- [ ] Worker-Job starten
- [ ] Parallel Forum öffnen
- [ ] **Erwartet:** Fragen-Liste <2s

---

## M) Mobile (kritische Screens)

### M.1 — Landing iOS Safari
- [ ] Hero-CTA tap → Tool-Section scroll
- [ ] Keine Layout-Brüche

### M.2 — Upload iOS Safari
- [ ] „Datei auswählen" → File-Picker
- [ ] Camera-Picker für Belege
- [ ] Pflicht-Files sichtbar markiert

### M.3 — Recall iOS Safari
- [ ] Token-Modal öffnet vollständig
- [ ] Input fokussiert, virtuelle Tastatur ok
- [ ] „Code prüfen" reachable

### M.4 — Result iOS Safari
- [ ] Hero-Betrag groß lesbar
- [ ] Chat-Input ohne Tastatur-Konflikt

---

## S) Support

### S.1 — Support-Modal öffnen
- [ ] Nav-Link „Support" → Modal
- [ ] Felder: Name, E-Mail, Nachricht

### S.2 — Support-Nachricht senden
- [ ] Ausfüllen + Submit
- [ ] **Erwartet:** Erfolgs-Banner

### S.3 — Support während failed_support
- [ ] Recall failed_support-Token
- [ ] Support-Button prominent in Result-Panel
- [ ] Klick → Modal mit voreingeparten Daten

---

## D) Datenschutz / Edge-Cases

### D.1 — LocalStorage clear
- [ ] DevTools localStorage komplett löschen
- [ ] **Erwartet:** Page funktioniert wie für neuen User

### D.2 — Token-Expiry (24h+)
- [ ] (Setup: 24h+ alter Token)
- [ ] Recall → friendly Fehler

### D.3 — Cookie-Consent (falls vorhanden)
- [ ] Datenschutz-Page erreichbar

---

## V) Verifikations-Checkliste vor Beta

- [ ] Alle R.* grün
- [ ] Alle P.* grün
- [ ] Alle F.* grün
- [ ] Alle C.* grün
- [ ] Alle U.* grün
- [ ] Alle A.* grün
- [ ] Alle M.* grün
- [ ] Alle S.* grün

**Beta-Launch erst bei 100% grün.**
