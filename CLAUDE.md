# AeroTax Backend — Arbeitsweise

## v8.23 Release-Stubs (offene Lücken — nicht produktionsreif)

Diese Stubs sind absichtlich **nicht voll implementiert**. User-facing wird das ehrlich kommuniziert; release-blocking entscheidet der Produkt-Eigentümer:

1. **Document-Replacement selektiver Re-Read** — `/api/job/<id>/upload-replacement` nimmt Datei entgegen, setzt `pending_reread=True` im Job-State. **`/finalize-pdf` blockiert**, solange `pending_reread=True`. UI zeigt "Datei erhalten. Die erneute Auswertung ist noch nicht abgeschlossen." — KEINE Behauptung "Auswertung aktualisiert". Selektives Sonnet-Re-Read pro Doc-Typ (statt voller Pipeline) folgt in einer späteren Version.

2. **Marker-Lexikon Klassifikator-Integration** — `marker_lexicon.json` wird gepflegt (`_record_marker_learning`), `status='approved'` nach 3 Bestätigungen. **ABER:** Der Klassifikator nutzt approved-Marker beim NÄCHSTEN Job aktuell **nicht aktiv** (würde Reader/Klassifikator-Refactor erfordern). User-facing wird das ehrlich kommuniziert: "Für diese Auswertung berücksichtigt. Als Lernkandidat gespeichert." — KEIN Versprechen "Beim nächsten Mal automatisch erkannt".

3. **Side-Drawer mit live-sichtbarer Result-Card** — Chat öffnet als rechts-fixierter Glassmorphism-Drawer auf Desktop (>900px), Modal auf Mobile. Die darunterliegende Result-Card ist **nicht parallel sichtbar**. Stattdessen: **Live-Betrag im Chat-Header** während Drawer offen (Polish-Workaround).

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

## Architektur-Grundsatz (v11 Clean-Release, gültig ab 2026-05-20)

> **Sonnet liest 3 Dokumente strukturiert. Backend matcht CAS+SE pro Datum, klassifiziert deterministisch, prüft Plausi und Health.**

Pflichtbasis (3 Dokumente + Formularangaben):
1. **Lohnsteuerbescheinigung** (LSB) — 1 PDF, Brutto/Jahres-/AG-Erstattung
2. **Streckeneinsatz-Abrechnungen** (SE) — ideal 12 Monate, Spesen + AG-gezahlt
3. **Dienstplan / CAS** (PUB/NTF) — ideal 12 Monate **mit Uhrzeiten**, Touren
4. Formular: Steuerjahr, Homebase, Entfernung km, optional Fahrzeit, optional Zusatzkosten

**Flugstundenübersicht ist KEINE Pflicht-/Reader-/Plausi-Quelle mehr.** Sie wird beim Upload als `legacy_ignored_flight_hours_summary` markiert, die Auswertung benutzt sie nicht. Die alten DP-Reader-Funktionen (`_parse_flugstunden_deterministic`, `parse_dienstplan_mit_ki`, `_sonnet_read_dp_structured*`) sind hart deaktiviert — sie werfen `RuntimeError` ausser bei explizitem Forensik-Override (`AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1`).

**Einsatzplan ist aus dem Produkt entfernt — nicht wieder als Pflichtdokument einführen.**

Pipeline: `_sonnet_read_lsb_v2` → `_sonnet_read_se_structured` → `_sonnet_read_cas_structured` (Reader V2) → `_classify_v11_cas_pipeline` → normalized_tours → tour-aware classification → BMF/Counter. Kein Opus-Hauptklassifikator. Kein produktiver Fallback auf Flugstundenübersicht.

### FollowMe.aero
FollowMe-Daten sind **Referenz/Benchmark**, KEINE Primärquelle. Bei Konflikten gewinnen CAS+SE+Plausi. Abweichungen werden im Audit (`CAS_FOLLOWME_DISAGREEMENT_AUDIT.md`) dokumentiert, nicht still angepasst.

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
- Cloud-Run-Logs ziehen, Deploys triggern (`gcloud run deploy … --source .`), Env-Vars *hinzufügen* via `--update-env-vars` (z.B. `PYTHONUNBUFFERED`)
- `python3 -m py_compile` als Sanity-Check

### Vorher fragen (große Änderungen)
- Neue Endpoints / neue Routen / neue Features
- Refactors die >3 Dateien oder >100 Zeilen anfassen
- `requirements.txt` Versions-Bumps oder neue Dependencies
- Frontend-Code spontan ändern oder von dir nicht angefragte Edits an `~/Desktop/site/`
  (wenn der Nutzer aber explizit eine Frontend-Änderung anfragt: ohne Rückfrage ändern + `wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true` ausführen)
- Cloud-Run-Env-Vars *löschen* (`--remove-env-vars`) oder existierende Werte *überschreiben* (`--set-env-vars`)
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

- **Backend:** Flask (`app.py`, ~2100 Zeilen Single-File), gehostet auf **Google Cloud Run**
  - Service: `aerotax-backend`, Region: `europe-west3`
  - Build: Dockerfile (gunicorn, bindet auf `$PORT`)
  - GitHub-Repo: `https://github.com/remedioms/aerotax-backend` → Cloud Run Continuous Deployment ab `main`
  - (Render-Hosting entfernt — Migration auf Cloud Run abgeschlossen, „Phase B")
- **Frontend:** statisches `index.html` in `~/Desktop/site/` (kein Build-Step, kein Repo)
  - Cloudflare Pages Projekt: `aerosteuer`
  - Account-ID: `28a9e1f1409d83cc94ef2c12db769985`
  - Domains: `aerosteuer.pages.dev`, `aerosteuer.de`
  - Deploy-Methode: direct upload (ad_hoc, kein Git-Connect)
  - Wrangler v4.86 installiert; Token + Account-ID in `~/.zshrc` als `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID`
  - Deploy-Befehl: `wrangler pages deploy ~/Desktop/site --project-name aerosteuer --commit-dirty=true`
- **PDF-Verarbeitung:** pdfplumber für Text, ReportLab für Output, PIL+pillow-heif für Bilder
- **KI:** Claude Sonnet 4.5 via `anthropic` SDK — aktive Stellen:
  - `parse_lohnsteuerbescheinigung` (LSB)
  - `parse_streckeneinsatz_mit_ki` (SE — Hybrid Regex+KI)
  - `_sonnet_read_cas_structured` + Reader V2 (CAS-Dienstplan)
  - `_resolve_uncertain_fact_with_ai` (KI-Resolver für NEEDS_AI-Fälle)
  - `parse_optionale_belege` (optionale Belege Vision)
  - `infer_missing_data_with_ki` (Schätzung wenn was fehlt)
  - DEPRECATED: `parse_dienstplan_mit_ki` (Flugstundenübersicht) — hart deaktiviert, nur via Forensik-Override.

## Deploy-Workflow (Google Cloud Run)

1. Code-Änderung → `git push origin main`
2. Cloud Run Continuous Deployment (GitHub-Trigger) baut die Dockerfile-Revision und deployt sie (Cloud Build ~3-5 Min).
   - Alternativ direkt: `gcloud run deploy aerotax-backend --source . --region europe-west3`
3. Env-Vars: NUR `--update-env-vars` / `--remove-env-vars` (siehe Warnung unten), nie `--set-env-vars`.

## Cloud Run env-Vars — VORSICHT

**WICHTIG (BUG-005 self-inflicted lessons-learned, 2026-05-12):**

- `gcloud run services update --set-env-vars="K=V"` **ÜBERSCHREIBT ALLE env vars** (lässt nur das gegebene Set übrig). Niemals für inkrementelle Änderungen nutzen.
- `gcloud run services update --update-env-vars="K=V"` **merged** in das existierende Set. Das ist der sichere Default für single-var updates.
- `gcloud run services update --remove-env-vars="K"` löscht eine einzelne Variable, lässt alle anderen.

Tat sich am 2026-05-12 ein P0-Incident weil ich `--set-env-vars="_BUG_002_REDEPLOY=…"` als Force-Restart-Trick nutzte — und dabei `AEROTAX_EXECUTION_MODE=cloud_tasks` plus alle anderen `AEROTAX_*`-Werte mitlöschte. Resultat: Backend fiel in thread-mode, der Worker-Thread lief wieder, Container-Restart-Loop kam zurück. Recovery brauchte ~10 Minuten + manuelle env-restoration aus alter revision.

**Force-Restart ohne env-Risiko:** `gcloud run deploy aerotax-backend --source . --region europe-west3` triggert eine neue Source-Build-Revision, ohne env-Vars anzufassen.

## Logs-Zugriff (Cloud Run)

`gcloud run services logs read aerotax-backend --region europe-west3 --limit 100`
(oder Cloud-Logging-Console). Ein Abruf zum Verifizieren genügt — keine Wiederholungs-Polls.
