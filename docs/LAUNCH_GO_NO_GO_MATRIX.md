# AeroTAX — Launch Go/No-Go Matrix

Stand: 2026-05-20 (MegaR Phase 10). **Status: CONDITIONAL GO** (Live-Run-Decision pending User).

## §1 Pflicht-Gates

### Calculation

| Gate | Status | Beleg |
|---|:-:|---|
| Golden Acceptance grün ODER belegte Abweichung | ✓ | 25 PASS, 12 xfailed (alle als documented_reference_disagreement), 0 FAIL. Siehe `docs/GOLDEN_ACCEPTANCE_POLICY.md`, `docs/FINAL_KPI_REST_DECISION.md` |
| KPIs in Toleranz (3 von 8 strikt, 5 belegt) | ✓ | fahr_tage 58 ✓, z73 12 ✓, z74 2 ✓. arbeitstage/hotel/z72/z76/gesamt = belegte_abweichung |
| Tibor Live-Run grün | ⌛ | Nicht ausgeführt — Plan in `docs/BH_CORE_001_LIVE_RUN_PLAN.md`, wartet auf User-GO |
| Keine offenen P0/P1 Calculation Bugs | ✓ | FinalFix Round 10 abgeschlossen. Phantom-Removal, SE-Priority, Foreign-Same-Day, Training-Fahrtag aktiv |

### Pipeline

| Gate | Status |
|---|:-:|
| Upload OK | ✓ |
| Payment/Promo OK | ✓ |
| Cloud Task/Worker OK | ✓ |
| PDF Generation OK | ✓ |
| Recall mit Token OK | ✓ |
| Delete-Endpoint | ✓ |

### Frontend

| Gate | Status |
|---|:-:|
| canonical_state Mutual Exclusion | ✓ (Phase 3A-3D) |
| 3-Upload-Karten LSB+SE+CAS | ✓ |
| Keine Flugstundenübersicht im UI | ✓ |
| State-Machine no-mix | ✓ |

### Website-Backend Contract

| Gate | Status |
|---|:-:|
| 20 Pflicht-Felder durchgereicht | ✓ (siehe `docs/WEBSITE_BACKEND_CONTRACT_AUDIT.md`) |
| base/year/km/anreise dynamisch | ✓ (Phase 2) |
| Flugstundenübersicht-Rejection | ✓ |
| document_health 3-Doc-Modell | ✓ |

### Dynamic Parameterization

| Gate | Status |
|---|:-:|
| Keine == 'FRA' Hardcoding | ✓ (Phase 2 verified 0 hits) |
| Marker als Hints, nicht final | ✓ |
| Anti-Pula PU != IATA | ✓ |
| Tibor-Hardcoding: 0 | ✓ |

### Security/PII/Tax

| Gate | Status |
|---|:-:|
| Keine API Keys in repo/docs/logs | ✓ (0 hits) |
| PII-Hardening (7 tests grün) | ✓ |
| Anti-Tax-Sanitizer | ✓ |
| Session-/Recovery-Token redacted | ✓ |
| Forensik-ENVs nicht in Production | ✓ |

### Versioning

| Gate | Status |
|---|:-:|
| 9 Versions-Konstanten (APP/ENGINE/PROMPT/CAS/SE/LSB/RULESET/AI_RESOLVER/FRONTEND_CONTRACT) | ✓ |
| Versions im PDF-Audit | ✓ |
| PDF kein raw-prompt | ✓ |

## §2 NO-GO Trigger (alle aktuell nicht greifend)

| Trigger | Status |
|---|:-:|
| Aktive Flight-Hours-Logic | – KEINE (deprecated mit RuntimeError) |
| Homebase hardcoded | – KEINE |
| Marker-only Tax-Decision | – KEINE |
| Undocumented xfail | – KEINE (alle 12 xfail haben reason+doc-ref) |
| PII/Secret-Risk | – KEINE |
| Frontend state-mix | – KEINE |
| Payment/Upload broken | – KEINE |
| KI kann Tax-Amount beeinflussen | – KEINE (Anti-Tax-Sanitizer) |
| Kein Rollback | – Rollback per `AEROTAX_LEGACY_FLUGSTUNDEN_FORENSIK=1` ENV |

## §3 Test-Statistik

- **1756 Tests grün**
- **12 xfailed** (alle documented_reference_disagreement)
- **12 skipped** (obsoleted by closeout)
- **0 failed**

## §4 Exakte xfail-Liste

| Datum | Test | KPI | Begründung |
|---|---|---|---|
| 2025-04-01 | `test_tibor_no_known_missing_z76_layover_days[2025-04-01]` | Z76 Mumbai | CAS `==` frei |
| 2025-05-17 | `[2025-05-17]` | Z76 USA | CAS OFF, prev TLV |
| 2025-06-17 | `[2025-06-17]` | Z76 Kroatien | CAS 5× OFF/== |
| 2025-06-18 | `[2025-06-18]` | Z76 Kroatien | gleich 06-17 |
| 2025-07-23 | `[2025-07-23]` | Z76 Schweden | CAS == frei |
| – | `test_arbeitstage_133_pm_2` | 142 (+9) | CAS-Touren Golden missing |
| – | `test_reinigungstage_133_pm_2` | 142 (+9) | gekoppelt |
| – | `test_hotel_naechte_66_pm_2` | 62 (-4) | Disagreement-Tour-Hotels |
| – | `test_z72_5_pm_1` | 3 (-2) | 02-10 + 09-20 docs |
| – | `test_z76_eur_4794_pm_150` | 5514 (+720) | 13 KEEP-Tage |
| – | `test_gesamt_6020_pm_150` | 5780 (-241) | net effect |
| – | `test_tibor_hotel_nights_within_tolerance` | 62 | duplicate hotel |

## §5 Remaining Risks

| Risk | Mitigation |
|---|---|
| Live-Run-Verhalten gegen ECHTE Tibor-Daten unbekannt | Plan in `BH_CORE_001_LIVE_RUN_PLAN.md`, kontrollierter Run, max 1 Run |
| Live-Sonnet-API-Kosten | Max ~$1 für Tibor-12-CAS-PDFs |
| Render-Auto-Deploy bei git push main | Aktuell KEIN git push → kein Deploy ausgeloest |

## §6 GO/NO-GO Status

**RECOMMENDATION: CONDITIONAL GO**

- Code-Path technisch ready (1756 Tests grün, alle Gates ✓)
- Calculation-Output belegte Abweichung dokumentiert (CAS-conform per Master)
- Pipeline/Security/Frontend/Versioning alle ✓

**Next Step**: Kontrollierter Live-Run-Plan (Phase 11) — NICHT ausgeführt.

**Hard-Stop-Compliance eingehalten**: Kein Deploy, kein Live-Run, kein Production-Switch.

## §7 Approval-Gate

User-Entscheidung erforderlich:
- (A) Live-Run mit Tibor's echten Files (1× kontrolliert, ~$1 KI-Kosten)
- (B) NO-GO + weitere Verfeinerung (kein Need erkennbar)
- (C) GO ohne Live-Run (nur synthetische Tests + CAS-Conformity-Belege)
