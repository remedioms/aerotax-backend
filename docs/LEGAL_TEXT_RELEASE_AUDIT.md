# Legal Text Release Audit

Stand: 2026-05-20 (Rel Phase 17).

## §1 Wording-Audit

| Text-Pflicht | Status | Beleg |
|---|:-:|---|
| "Keine Steuerberatung" | PASS | UI-Footer + PDF-Disclaimer |
| "Keine Garantie" | PASS | UI + PDF |
| "Kein prüfungsfest"-Behauptung | PASS | Per CLAUDE.md §Was-AeroTAX-NICHT-verspricht |
| "Werbungskosten-Aufstellung" | PASS | PDF-Header |
| User-Prüfpflicht | PASS | UI + PDF |
| BMF-Pauschalen als Grundlage | PASS | PDF-Audit-Section |
| AG-Erstattung-Hinweis | PASS | PDF |
| Datenschutz-Hinweise | PASS | UI-Footer |
| Löschung/Retention | PASS | Operations Runbook + UI-Footer |
| Zahlungs-Refund-Hinweise | PASS | Operations Runbook |
| Support-Hinweise | PASS | UI-Help |

## §2 §9 EStG Entfernungspauschale

| Regel | Implementation |
|---|---|
| 0,30 €/km bis 20 km | ✓ |
| 0,38 €/km ab 21. km (gilt 2022-2026) | ✓ |
| Einfache Entfernung | ✓ |
| AG-Erstattung Anrechnung | ✓ Jobticket/Z17 |
| Werbungskostenpauschale 1230€ | implizit (User-Werbungskosten + Pauschalvergleich) |

## §3 Verpflegungsmehraufwand

| Regel | Implementation | Risk |
|---|---|---|
| Inland Z72/Z73/Z74 | ✓ | low |
| Ausland Z76 | ✓ BMF-Tabelle | low |
| 3-Monats-Frist (§9 Abs. 4a EStG) | NICHT umgesetzt | NEEDS_DECISION (siehe §4) |
| AG-Erstattung Anrechnung | ✓ Z77-Topf | low |

## §4 3-Monats-Frist Risk

§9 Abs. 4a Satz 6 EStG: Wenn Crew länger als 3 Monate am selben Ort tätig ist, entfällt Z76 ab Monat 4.

**Aktueller Stand**: AeroTAX zählt JEDEN Tour-Tag als Z76-fähig, ohne Monatszählung pro Ort.

**Risikobewertung**: 
- Crew-Auswärtstätigkeit ist typischerweise <90 Tage am selben Ort (Touren wechseln häufig).
- Edge-Case wenn längere Stationierung (z.B. Sim-Training in einer Stadt 4+ Monate).

**Decision**: 
- **NEEDS_DECISION** für nächste Major-Version
- Aktuelle Limitation in PDF dokumentieren: „AeroTAX zählt Verpflegungsmehraufwand pro Auswärtstätigkeit. Bei längerer Tätigkeit am selben Ort (über 3 Monate) prüfe Steuerberater wegen §9 Abs. 4a EStG-Dreimonatsfrist."

## §5 Flugpersonal-Sonderlogik

| Aspekt | AeroTAX | Begründung |
|---|---|---|
| Homebase = erste Tätigkeitsstätte | dynamisch aus form['base'] | Crew-Standard |
| Flug-Crew als "Auswärtstätigkeit"-Berufsgruppe | ✓ implizit | Flugpersonal-Typische Werbungskosten |
| Reinigungspauschale Crew-Uniform | ✓ pro Arbeitstag | LStR-Pauschale |
| Trinkgeld-Annahme | ✓ pro Hotelnacht | Standard-Crew-Praxis |

**Disclaimer**: „AeroTAX rechnet nach Flugpersonal-Standard-Werbungskosten. Bei Sonderfällen Steuerberater konsultieren."

## §6 Cookie / Tracking

| Pflicht | Status |
|---|:---:|
| Privacy-Banner | NEEDS_DECISION (per Cloudflare-Pages-Default keine) |
| Stripe-Iframe DSGVO | ✓ Stripe-Compliant |
| Anthropic API DSGVO | ACCEPTED_RISK — Anthropic AG-AVV erforderlich |

## §7 Output-Text-Review

| Text | Location | Status |
|---|---|---|
| "Werbungskosten-Aufstellung Steuerjahr {year}" | PDF-Header | PASS |
| "Keine Steuerberatung" | PDF-Footer + UI | PASS |
| "Diese Berechnung basiert auf deinen hochgeladenen Dokumenten" | PDF + UI | PASS |
| "Bei Unsicherheiten Steuerberater konsultieren" | PDF-Disclaimer | PASS |
| "BMF-Tabelle 2025" | PDF-Audit-Section | PASS |

## §8 Status

**Overall: PASS** mit:
- 2 NEEDS_DECISION (3-Monats-Frist, Cookie-Banner)
- 1 ACCEPTED_RISK (Anthropic-AVV)

Kein Launch-Blocker. Risiken in OPERATIONS_RUNBOOK_FINAL dokumentiert.
