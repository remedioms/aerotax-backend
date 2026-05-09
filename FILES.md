# AeroTax — Files & Architecture Guide

**Stand:** 2026-05-09 · Version 5.0 · Build `self-reflection-audit-pdf-dsgvo-tests`

Briefing für Mit-Entwickler (Mensch oder KI). Dieses Dokument erklärt was wo liegt und warum, damit ihr ohne wochenlanges Reverse-Engineering einsteigen könnt.

---

## Was ist AeroTax?

Online-Tool das Lufthansa-Kabinen- und Cockpitcrew aus ihren PDFs (Lohnsteuerbescheinigung, Streckeneinsatz, Dienstplan, Einsatzplan) eine Werbungskosten-Aufstellung für die Steuererklärung baut. Output: ein PDF mit allen Beträgen + WISO-Eintragungs-Anleitung.

Domain: aerosteuer.de (Frontend Cloudflare Pages) · Backend: aerotax-backend.onrender.com (Render Free)

**Wichtig:** Es ist ein **Berechnungs-Werkzeug**, kein Steuerberater-Ersatz. User muss Werte selbst prüfen.

---

## Tech-Stack

| Komponente | Tech | Hosting |
|---|---|---|
| Backend | Flask + gunicorn (Single-File `app.py`) | Render Free 512 MB |
| KI | Anthropic Sonnet 4.6 (Lesen) + Opus 4.7 (Klassifikation) | API |
| PDF-Parsing | pdfplumber (Text), ReportLab (Output), PIL+pillow-heif (Bilder) | — |
| Payment | Stripe (Payment Element) | — |
| Datenbank | Supabase (Postgres) | Cloud |
| Frontend | Statisches HTML+JS (kein Build-Step) | Cloudflare Pages |
| Tests | pytest | lokal |

---

## Repository-Layout

### `/app.py` (~7.700 Zeilen, Single-File-Backend)

Single-File-Architektur — bewusste Entscheidung weil Flask-App keinen großen Refactor-Bedarf hat. Sektionen:

- **Z. 1-100:** Imports, Konstanten (Stripe-Keys, FRONTEND_URL, ANTHROPIC_KEY)
- **Z. 100-650:** Stripe-Routen (`/api/create-payment-intent`, `/api/upload-files`, `/api/stripe-webhook`)
- **Z. 650-770:** Job-Persistierung auf Disk (überlebt Render-Restart), Worker-Queue
- **Z. 770-820:** `_audit()` Helper für Audit-Trail
- **Z. 820-1060:** `process_real()` Endpoint + `_run_process_async()` Background-Worker
- **Z. 1060-1240:** `/api/job/<id>` Status, `/api/job/<id>/audit` Detail-Audit
- **Z. 1240-1500:** Cleanup-Loops (alte Jobs, Sessions, PDFs)
- **Z. 2500-2750:** BMF-Pauschalen-Tabellen (Inland/Pendler/Reinigung/Trinkgeld) **mit Review-Block**
- **Z. 2870-3700:** Alter Multi-Parser-Pipeline-Code (Legacy, Fallback)
- **Z. 4350-4500:** `_opus_final_audit()` (alte Audit-Funktion, wird im Hybrid-Pfad nicht mehr genutzt)
- **Z. 4800-5400:** **HYBRID-ARCHITEKTUR (aktueller Hauptpfad):**
  - `_sonnet_read_lsb_v2()` — Sonnet liest LSB-PDFs via Tool-Use (Z. 4801)
  - `_sonnet_read_se_summary_v2()` — Sonnet aggregiert SE-Summen (Z. 4955)
  - `_opus_classify_days_v2()` — Opus klassifiziert Tag-für-Tag (Z. 5144)
  - `_detect_classification_issues()` — Math-Invarianten-Check (Z. 5410)
  - `hybrid_analyze()` — Sequenzieller Orchestrator + Self-Reflection-Loop (Z. 5460)
  - `_berechne_via_hybrid()` — Komplettes Result-Dict bauen (Z. 5530)
  - `berechne()` — Hauptpfad mit Hybrid + Fallback (Z. 5800)
- **Z. 5900-6700:** Alte Multi-Parser `berechne()`-Implementation (Fallback, falls Hybrid crasht)
- **Z. 6800-7700:** `erstelle_pdf()` — ReportLab PDF-Generator mit Cover, WISO-Anleitung, Tag-für-Tag-Audit-Tabelle, Belege

### `/referenz_faelle.txt` (~570 Zeilen)

**Wissens-Buch** das in den Opus-Klassifikations-Prompt geladen wird. Konsolidiertes Wissen aus wochenlanger Konversation:
- Section 1: Tour-Klassifikation (Z72/Z73/Z76 Decision-Tree)
- Section 2: §9 + §3 EStG
- Section 3: EASA-FTL EU 965/2012
- Section 4: LH-Marker-Katalog
- Section 5: BMF-Auslands-Pauschalen-Tabelle
- Section 6: SE-Format-Spezifikation
- Section 7: Briefingzeiten-Faustregel
- Section 8: Pauschalen-Berechnung
- Section 9: Patterns (NICHT konkrete Reference-Cases — Anti-Overfit refactored 2026-05-09)
- Section 10: 19 häufige Fehler
- Section 11: Multi-LSB / Teilzeit / Edge-Cases
- Section 12: Self-Check-Liste vor Tool-Aufruf

### `/bmf_data.py` (~1.400 Zeilen)

Auslandsspesen-Tabelle pro Land (BMF-Schreiben jährlich). `BMF_AUSLAND_BY_YEAR` + `IATA_TO_BMF`-Mapping. **Reviewed: 2026-05-09**, next review Dec 2026.

### `/tests/test_calculation.py` (164 Zeilen)

17 Pure-Python-Unit-Tests (kein Netzwerk, keine KI):
- Pendlerpauschale-Staffelung
- BMF-Tabellen-Vollständigkeit
- Math-Konsistenz-Check (`_detect_classification_issues`)
- PII-Redaktion (`_redact_pii`)
- Pauschalen-Konstanten

Lokal: `python3 -m pytest tests/ -v` — alle grün.

### `/scripts/generate_action_guide.py`

Erzeugt `~/Desktop/AeroTax_Action_Guide.pdf` (10-seitige Anleitung welche Aktionen der Eigentümer selbst tun muss).

### `/CLAUDE.md`

Arbeitsweise-Vorgaben für Claude Code — wann ohne Rückfrage und wann mit Rückfrage agiert wird.

### `/Procfile` + `/requirements.txt` + `/Dockerfile` + `/fly.toml`

Render-/Heroku-Deploy-Konfiguration. `Dockerfile` und `fly.toml` historisch (Migration von Fly.io → Render), nicht aktiv genutzt.

### `/supabase_schema.sql`

Tabellen-Definition für `uploaded_files`, `pdfs`, `sessions`, `support_tickets`. Postgres-Schema in Supabase deployed.

---

## `/Users/miguelschumann/Desktop/site/` — Frontend

### `/index.html` (~6.300 Zeilen)

Statisches Single-Page (kein Build, kein Repo). Sektionen:
- **Z. 1-1500:** CSS (selbst-geschrieben, Glassmorphism-Style, Dark-Theme)
- **Z. 1500-2050:** Hero + How-It-Works + Tool-Sektion (Upload-Karten, Form-Felder)
- **Z. 2050-2700:** Animation-Steps + JavaScript-Logic (calculate, payment, polling)
- **Z. 2700-4500:** PDF-Result-Rendering, Optionale-Belege-Karten, Belege-Upload
- **Z. 4500-5800:** Modals (Impressum, Datenschutz, AGB, Forum, Kontakt)
- **Z. 5800-6300:** Footer + Tooltips

Deploy via:
```bash
wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true
```

Cloudflare Pages: Account `28a9e1f1409d83cc94ef2c12db769985`, Domains `aerosteuer.pages.dev` + `aerosteuer.de`.

---

## Datenfluss

```
1. User → Frontend (aerosteuer.de)
2. Frontend → /api/create-payment-intent → Stripe → User pays
3. Frontend → /api/upload-files (PDFs nach Bezahlung) → Supabase + In-Memory
4. Frontend → /api/process → Job in Queue
5. Worker → berechne() → hybrid_analyze():
     a. Sonnet → _sonnet_read_lsb_v2() → LSB-Werte
     b. Sonnet → _sonnet_read_se_summary_v2() → Z77 + Auslandsspesen
     c. Opus → _opus_classify_days_v2() → Tag-für-Tag-Klassifikation
     d. _detect_classification_issues() → bei Verletzung: Opus-Recheck
6. Worker → erstelle_pdf() → PDF in Supabase
7. Frontend pollt /api/job/<id> → bekommt download_url
8. User → /api/download/<token> → PDF
9. Files purged (60s nach PDF-Generierung)
```

---

## Hybrid-Architektur (Wichtig zu verstehen)

**Warum Hybrid?** Pro Crew-Auswertung läuft die Klassifikation in 3 Schritten **sequenziell** (nicht parallel — Memory-Schonung Render Free 512 MB):

1. **Sonnet liest LSB** (kleinster Footprint) → Zahlen aus Lohnsteuerbescheinigung
2. **Sonnet aggregiert SE-Summen** mit monatlicher Cross-Verifikation (Bug-Schutz)
3. **Opus klassifiziert Tag-für-Tag** mit dem Wissens-Buch im Prompt

Nach Schritt 3 läuft **Self-Reflection-Loop** (`_detect_classification_issues`):
- Wenn Z76 > Z77 (mathematisch unmöglich) → Opus-Recheck
- Wenn Z76 vs Auslandsspesen ±40% abweichend → Opus-Recheck
- Wenn Hotel > Arbeitstage → Opus-Recheck
- Recheck-Pass bekommt konkreten Korrektur-Auftrag-Prompt

---

## Audit-Trail

Jeder Job hat einen Audit-Trail (`/api/job/<id>/audit`):

```json
{
  "audit": [
    {"event": "job_created", "ts": "...", "data": {...}},
    {"event": "calculation_started", "ts": "..."},
    {"event": "file_validation_warnings", "ts": "...", "data": {"warnings": [...]}},
    {"event": "calculation_done", "ts": "...", "data": {"gesamt": 5743, ...}},
    {"event": "classification_detail", "ts": "...", "data": {
       "summary": {...},
       "nachweis": "JAN: ...",
       "tage_detail": [{"datum": "2025-01-03", "klass": "Z76", "begruendung": "BLR Indien 4T"}]
    }},
    {"event": "pdf_created", "ts": "...", "data": {"token": "..."}},
    {"event": "files_purged", "ts": "...", "data": {"note": "..."}}
  ]
}
```

PII (Identnr, Personalnummer etc.) wird vor Disk-Persist redacted (`_redact_pii`). Audit-Files auto-gelöscht nach 48h.

---

## Bekannte offene Bugs / TODOs

### Klassifikations-Genauigkeit
- Opus überzählt manchmal Z72 (Same-Day) statt Z73 (Inland-Übernachtung)
- fahr_tage tendiert zu hoch (zählt vermutlich Office-Tage doppelt)
- Self-Reflection-Loop ist neu (v5.0), nicht im echten Test verifiziert

### UX
- Kein Korrektur-Frontend (User kann Werte nicht editieren wenn KI falsch liegt)
- Onboarding-Walkthrough fehlt ("welche PDFs brauche ich, wo finde ich sie")

### Compliance
- Steuerberater-Review noch nicht durchgeführt
- AVVs noch nicht alle unterzeichnet (Anthropic, Stripe, Supabase, Render)
- Keine Berufshaftpflicht-Versicherung

### Infra
- Sentry/Logging fehlt
- Render Free 512 MB → bei Lastzeit OOM-Risiko
- Frontend in `~/Desktop/site/` ist NICHT in einem Git-Repo (nur Backup auf der Maschine)

Siehe `~/Desktop/AeroTax_Action_Guide.pdf` für komplette Action-Liste.

---

## Lokal entwickeln

```bash
cd ~/Desktop/aerotax-backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Tests
python3 -m pytest tests/ -v

# Backend lokal (braucht .env mit ANTHROPIC_API_KEY etc.)
flask run --port 8080

# Compile-Check
python3 -m py_compile app.py
```

---

## Deploy-Workflow

**Backend:**
```bash
git push origin main  # Render auto-deployt (~3-4 Min Build)
```

Bei Env-Var-Änderungen: manuell Deploy triggern via Render API:
```
POST https://api.render.com/v1/services/srv-d7o6qbe8bjmc7398acdg/deploys
```

**Frontend:**
```bash
wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true
```

---

## Versions-History (relevante Highlights)

- **v5.0** (2026-05-09) — Self-Reflection-Loop, Tag-für-Tag-Audit-PDF, DSGVO-PII-Redaktion, Test-Suite minimal, IP-Rate-Limit
- **v4.4** — Reference-Cases zu Patterns refactored (Anti-Overfit)
- **v4.3** — Audit-Tag-Detail + Z73-Decision-Tree
- **v4.2** — Sonnet-SE Monatlich-Cross-Check (Z77-Bug fix)
- **v4.1** — Wissens-Buch konsolidiert
- **v4.0** — Hybrid-Architektur (Sonnet+Opus sequenziell)

---

## Wenn ihr was ändern wollt

1. Erstmal `tests/test_calculation.py` laufen lassen — wenn rot, nicht weitermachen
2. Bei Klassifikations-Änderungen: `referenz_faelle.txt` ist single source of truth, nicht `app.py` Prompts
3. Bei BMF-Änderungen: `bmf_data.py` UND `BMF_INLAND_BY_YEAR` in `app.py`
4. Bei Frontend-Änderungen: vorsichtig mit Marketing-Versprechen — siehe Disclaimer-Verschärfung 2026-05-09
5. Compile-Check vor jedem Push: `python3 -m py_compile app.py`
6. Keine PII in Logs, keine API-Keys im Code

Bei Fragen: schaut in die git log Messages — Miguel + Claude Code dokumentieren detailliert was warum geändert wurde.
