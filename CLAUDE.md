# AeroTax Backend — Arbeitsweise

## Architecture principle

> **Sonnet reads facts. Python classifies and calculates. ReportLab renders. No AI-generated tax decision is accepted as final.**

> **Die Berechnung ist deterministisch und auditierbar. Die Genauigkeit hängt von der Lesbarkeit und Vollständigkeit der hochgeladenen Dokumente ab. Unklare Stellen werden im Audit sichtbar gemacht — sie werden nicht still als "richtig" angenommen.**

**Was AeroTAX NICHT verspricht:**
- "100% sicher" / "Steuerberater-sicher" / "Finanzamt-sicher" — solche Aussagen erscheinen NIE in UI/PDF/Marketing
- "Garantiert korrekt" — die Genauigkeit hängt am Sonnet-Lesergebnis und an der Vollständigkeit der hochgeladenen Dokumente
- Konkrete Prozent-Zahlen (z.B. "95%") — die kann niemand seriös prüfen

**Was AeroTAX kann:**
- Deterministische Klassifikation (gleicher Reader-Input → gleicher Output)
- Vollständiger Audit-Trail (jeder Tag mit reader_facts + classifier_result + diagnostics)
- Sichtbare Unklarheiten (vma_unmapped_se, unresolved_days, hotel_candidate_issues, iata_unknown, bmf_missing)
- Health-Status (green/yellow/red) der dem User sagt: "Hier solltest du prüfen"

Strikte Verantwortungs-Trennung — keine Vermischung:

| Schicht | Verantwortung | Was sie NICHT macht |
|---|---|---|
| **KI/Sonnet (Reader)** | Strukturierte Lese-Fakten extrahieren: `activity_type`, `routing`, `overnight_after_day`, `layover_ort`, `start_time`, `end_time`, `stfrei_betrag`, `stfrei_ort`, `storno`, `brutto`, `Z17`, etc. | KEINE steuerliche Klassifikation. KEIN Z72/Z73/Z74/Z76. KEINE finale Beträge. Reader-Tools enthalten keinen Z-Code im enum. |
| **Python (Classifier)** | Tag-Klassifikation, Tour-Cluster, Fahrtage, Arbeitstage, Hotelnächte, BMF-Landmapping, VMA-Beträge, Z17/Z77-Topftrennung, finaler WISO-Gesamtbetrag. Deterministisch — gleiche Input-Fakten → gleicher Output. | Keine Lese-Heuristik auf rohen Dokumenten — Python rechnet nur mit Reader-Fakten. |
| **ReportLab (Renderer)** | PDF-Layout aus Result-Dict | Keine Berechnung. Keine Klassifikation. |

**Konsequenz:** Wenn ein Wert falsch ist, ist sofort sichtbar wer Schuld hat:
- Reader-Fakt falsch → `tage_detail[i].reader_facts` zeigt was Sonnet gelesen hat
- Classifier falsch → `tage_detail[i].classifier_result.reason` zeigt Code-Pfad
- BMF-Mapping fehlt → `tage_detail[i].diagnostics.bmf_mapping_issue` listet Lücke
- PDF zeigt falsch → result-Dict war richtig, ReportLab-Bug

## Architektur-Grundsatz (v8.0)

> **Sonnet liest 3 Dokumente strukturiert. Backend matcht DP+SE pro Datum, klassifiziert deterministisch, prüft Plausi und Health.**

Pflichtbasis (3 Dokumente + Formularangaben):
1. Lohnsteuerbescheinigung
2. Flugstundenübersicht
3. Streckeneinsatzabrechnung
4. Formular: Steuerjahr, Homebase, Entfernung km, optional Fahrzeit, optional Zusatzkosten

**Einsatzplan ist aus dem Produkt entfernt — nicht wieder als Pflichtdokument einführen.**

Pipeline: `_sonnet_read_lsb_v2` → `_sonnet_read_se_structured` → `_sonnet_read_dp_structured` → `_document_health_check` → `_match_dp_se_per_day` → `_build_tour_clusters` → `_deterministic_classify_v7`. Kein Opus-Hauptklassifikator. Kein produktiver Fallback.

### v8-Garantien
- Jede aktive SE-Zeile landet in Z72/Z73/Z74/Z76 oder `vma_unmapped_se` (sichtbarer Issue, kein stilles Sonstiges).
- Klass `Issue` statt stillen Sonstiges für nicht klassifizierbare Tage.
- `audit_notes` (informativ, Ergebnis berechnet) ↔ `unresolved_days` (echte offene Probleme) getrennt.
- Document Health Check vor Berechnung: red → keine Auswertung mit klarem User-Text.
- Hard-Fails: hotel_naechte > arbeitstage; arbeitstage > 230.
- Reader-/Engine-Versionen im Audit (`READER_VERSIONS`, `ENGINE_VERSION`, `PROMPT_VERSION`).
- Counter aus `tage_detail.klass` aggregiert — kein inkrementelles Hochzählen im Loop.
- Fahrtage = `Σ dp.requires_commute`. Heimkehr/Layover/Tourfortsetzung zählen NICHT.
- ZeroDay zählt nur als Arbeitstag wenn `dienstlich=True` (Same-Day < 8h, isolierter Tour-Tag).
- Hotel-Nächte: nur Z73/Z74/Z76 mit overnight=True UND Layover-Ort ≠ Homebase.

### EASA/FTL — nur Lesehilfe
AeroTAX verwendet EASA-/FTL-Begriffe nur als Lesehilfe für Dienstplan-Marker. AeroTAX prüft keine Flugdienstzeit-Compliance.

### Homebase-Logik
Homebase = der Flughafen, dem das Crewmitglied dienstlich zugeordnet ist. **Nicht** der nächstgelegene oder Wohnort-Flughafen. FRA wird **nicht** hardcoded — alle `starts_at_homebase`/`ends_at_homebase`/Hotel-Vergleiche prüfen gegen die im Formular gewählte Homebase. FRA bei MUC/BER/DUS-Base ist ein normaler Routing-Flughafen, kein Homebase-Indikator.

### Reference-Contract (anonymisiert)
`tests/test_calculation.py` enthält `REFERENCE_CONTRACT_2025_MIGUEL` als Test-Constant für gezielten Diff-Vergleich. Werte werden **nicht** im Produktions-Code hardcoded — nur als Soll-Werte für Live-Job-Vergleich (`reference_diff`-Helper). Plus Tag-Listen `REFERENCE_FAHRTAGE_2025_MIGUEL` und `REFERENCE_DEUTSCHLAND_14_2025_MIGUEL` für Tag-für-Tag-Diff.

## Autonomie-Modus

Der Nutzer will **autonom** arbeiten lassen außer bei großen Änderungen.

### Ohne Rückfrage (einfach machen)
- Bug-Fixes in bestehenden Funktionen
- Logging hinzufügen/verbessern (`print`-Statements, Log-Ebenen)
- Prompt-Tuning für Claude (Wording in `parse_*_mit_ki` Funktionen)
- Variable-Renames, Tippfehler, Kommentar-Updates
- Nach Code-Änderungen: `git add <file> && git commit -m "..." && git push` ohne zu fragen
- Render-Logs ziehen, Render-Deploys triggern, Render-Env-Vars *hinzufügen* (z.B. `PYTHONUNBUFFERED`)
- `python3 -m py_compile` als Sanity-Check

### Vorher fragen (große Änderungen)
- Neue Endpoints / neue Routen / neue Features
- Refactors die >3 Dateien oder >100 Zeilen anfassen
- `requirements.txt` Versions-Bumps oder neue Dependencies
- Frontend-Code spontan ändern oder von dir nicht angefragte Edits an `~/Desktop/site/`
  (wenn der Nutzer aber explizit eine Frontend-Änderung anfragt: ohne Rückfrage ändern + `wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true` ausführen)
- Render-Env-Vars *löschen* oder existierende Werte *überschreiben*
- Stripe-Webhook / Payment-Logik
- Datenbankschema (falls hinzukommt)
- Branch-Operationen außer `main` (rebase, force-push, branch-deletion)
- `rm -rf`, `git reset --hard`, `git push --force`

### Niemals (kein Override)
- `git push --force` auf `main`
- API-Keys oder Secrets in Logs/Output ausgeben
- Hooks via `--no-verify` umgehen
- `.env`, Credentials-Files committen

## Tech-Stack

- **Backend:** Flask (`app.py`, ~2100 Zeilen Single-File), gehostet auf **Render**
  - Service: `srv-d7o6qbe8bjmc7398acdg`, Owner: `tea-d7np5om8bjmc73909ea0`
  - URL: `https://aerotax-backend.onrender.com`
  - Auto-Deploy bei `git push origin main` aktiv
- **Frontend:** statisches `index.html` in `~/Desktop/site/` (kein Build-Step, kein Repo)
  - Cloudflare Pages Projekt: `aerosteuer`
  - Account-ID: `28a9e1f1409d83cc94ef2c12db769985`
  - Domains: `aerosteuer.pages.dev`, `aerosteuer.de`
  - Deploy-Methode: direct upload (ad_hoc, kein Git-Connect)
  - Wrangler v4.86 installiert; Token + Account-ID in `~/.zshrc` als `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`
  - Deploy-Befehl: `wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true`
- **PDF-Verarbeitung:** pdfplumber für Text, ReportLab für Output, PIL+pillow-heif für Bilder
- **KI:** Claude Sonnet 4.5 via `anthropic` SDK — vier Stellen:
  - `parse_lohnsteuerbescheinigung` (LSB)
  - `parse_streckeneinsatz_mit_ki` (SE — Hybrid Regex+KI)
  - `parse_dienstplan_mit_ki` (Flugstunden)
  - `parse_optionale_belege` (optionale Belege Vision)
  - `infer_missing_data_with_ki` (Schätzung wenn was fehlt)

## Deploy-Workflow

1. Code-Änderung → `git push origin main`
2. Render auto-deployt (Build ~3-4 Min, Deploy ~30s)
3. Bei Env-Var-Änderungen: manuell triggern via `POST /v1/services/.../deploys` (Render auto-redeployt nicht bei Env-Changes)

## Logs-Zugriff

Render API mit Token aus `RENDER_API_KEY` Env-Var (oder im Dashboard hinterlegt).
Endpoint: `GET https://api.render.com/v1/logs?ownerId=...&resource=srv-...&type=app`
Ein Test-Call zum Verifizieren genügt — keine Wiederholungs-Polls.
