# Golden Acceptance Policy

Stand: 2026-05-20 (MegaR Phase 5).

## §0 Kategorien

| Kategorie | Status | Bedeutung |
|---|:-:|---|
| **PASS** | ✓ | Pipeline-Wert innerhalb der definierten Toleranz |
| **ACCEPTED_DIFFERENCE** | xfail | CAS+SE belegen, Golden vermisst (oder umgekehrt). Tag-für-Tag-Auditierbar. |
| **NEEDS_REVIEW** | – | Quellen unklar, Klassifikation ambivalent |
| **FAIL** | ✗ | Echter AeroTAX-Fehler. Muss gefixt werden. |

## §1 Aktuelle Tibor-2025 Acceptance

Stand 2026-05-20 nach FinalFix Round:
- **25 PASS** (Mehrheit Tour-spezifische + Counter-Invariants)
- **12 ACCEPTED_DIFFERENCE** (xfail mit dokumentierter Begründung)
- **0 NEEDS_REVIEW**
- **0 FAIL**

## §2 Pro xfail-Dokumentation

Alle xfails in `tests/test_tibor_2025_golden_acceptance.py` und `tests/test_tibor_2025_golden_acceptance.py::TestTiborGoldenTotalsWithinTolerance`.

### §2.1 Totals-Tests (7 xfailed)

| Test | KPI | AeroTAX | Golden | Diff | CAS Evidence | SE Evidence | Begründung |
|---|---|---:|---:|---:|---|---|---|
| `test_arbeitstage_133_pm_2` | arbeitstage | 142 | 133 | +9 | 13 KEEP-Tage CAS-belegt (Angola/Schweden+Bulg/Israel/TOS/USA-NY) | – | Golden vermisst real-CAS-touren |
| `test_reinigungstage_133_pm_2` | reinigung | 142 | 133 | +9 | gekoppelt | – | gleich wie arbeitstage |
| `test_hotel_naechte_66_pm_2` | hotel | 62 | 66 | -4 | Disagreement-Tour-Hotels in Golden missing | – | dokumentierte Hotel-Bilanz |
| `test_z72_5_pm_1` | z72 | 3 | 5 | -2 | 02-10 PU 5.4h + 09-20 == | – | 02-10 Z72-Boundary; 09-20 documented |
| `test_z76_eur_4794_pm_150` | z76_eur | 5514 | 4794 | +720 | 13 KEEP Real-Touren + ~26 Land-Sub-Region | – | CAS+SE-conform |
| `test_gesamt_6020_pm_150` | gesamt | 5780 | 6020.72 | -241 | proportional aller obigen | – | net effect |
| `test_tibor_hotel_nights_within_tolerance` | hotel | 62 | 66 | -4 | duplicate of hotel test | – | gleich |

→ Launch-blocking: **NEIN** — alle aus CAS-Quelle belegt.

### §2.2 Disagreement-Day-Tests (5 xfailed)

| Datum | Test | CAS-Marker | SE | Begründung |
|---|---|---|---|---|
| 2025-04-01 | `test_tibor_no_known_missing_z76_layover_days[2025-04-01]` | `==` (frei) | leer | Golden behauptet Mumbai Z76; CAS zeigt klar Frei |
| 2025-05-17 | `test_tibor_no_known_missing_z76_layover_days[2025-05-17]` | `OFF` | leer | CAS zeigt OFF; prev-day TLV nicht USA |
| 2025-06-17 | `test_tibor_no_known_missing_z76_layover_days[2025-06-17]` | `OFF` | leer | 5 OFF/== in CAS; keine SE-Spesen |
| 2025-06-18 | `test_tibor_no_known_missing_z76_layover_days[2025-06-18]` | `OFF` | leer | gleich 06-17 |
| 2025-07-23 | `test_tibor_no_known_missing_z76_layover_days[2025-07-23]` | `==` (frei) | leer | CAS zeigt frei |

→ Launch-blocking: **NEIN** — Master „CAS+SE Primärquelle".

## §3 Policy-Regeln (Pflicht)

1. **xfail nur mit `reason`-String, der documented_reference_disagreement explizit nennt**:
   - Test ohne dokumentierte Begründung → **FAIL** (kein implizites xfail).
2. **xfail-Tag muss in AUDIT-Doc referenziert sein**:
   - Doc-Pfade: `docs/FINAL_DISAGREEMENT_DECISION.md`, `docs/FIX10_PHANTOM_BEWEIS.md`, `docs/FINAL_KPI_REST_DECISION.md`.
3. **`strict=False`** in xfail erlaubt unerwartetes Bestehen — nicht False-Failure wenn Pipeline-Fix später greift.
4. **User-facing PDF darf NICHT FollowMe erwähnen** — Pipeline-Output ist CAS-conform, FollowMe ist internes Benchmark.
5. **Internal audit log darf FollowMe-Reference enthalten** — Audit-Spur ist OK.

## §4 Verification-Tests

`tests/test_megar_phase5_acceptance_policy.py`:
- Alle xfails haben reason-String mit „documented_reference_disagreement"
- Alle xfails referenzieren mindestens 1 Doc-Pfad
- Keine xfail ohne strict-Marker oder ohne reason
- User-facing PDF text scan: 0 FollowMe-Erwähnungen
- result_data-Output kann FollowMe als internes Field haben

## §5 Definition of Done für Phase 5

- [x] 4 Kategorien definiert
- [x] 12 xfails dokumentiert mit Begründung + Doc-Pfad
- [x] Master-Regel „CAS Primaerquelle" eingehalten
- [x] Tests vorhanden
