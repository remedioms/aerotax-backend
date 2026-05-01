# AeroTax Backend — Arbeitsweise

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
- Frontend-Code ändern (Cloudflare Pages Repo)
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
- **Frontend:** Cloudflare Pages (Repo separat — Wrangler-CLI noch nicht eingerichtet)
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
