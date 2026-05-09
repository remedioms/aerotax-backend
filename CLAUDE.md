# AeroTax Backend — Arbeitsweise

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
