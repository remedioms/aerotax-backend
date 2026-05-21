# Highest-Defensible VMA — Final Report

Stand: 2026-05-21.
Produkt-Regel: "Highest defensible, source-backed amount."

## §1 Korrigierte Aussage (Ersatz für "Netto-Effekt = 0")

Bei Tibor reduziert Z77 (4 705 €) den größten Teil der VMA-Differenz, **aber
nicht alles**. Die FollowMe-Brutto-VMA übersteigt Z77, AeroTAX nicht — daher
asymmetrischer Clamp: FM netto-VMA = 341 €, AT netto-VMA = 0 €.

Vor diesem Fix-Set: AT-Netto = 976 € vs FM-Netto = 1 315.72 € → **Δ +339.72 €**.

## §2 Revised Source Arbitration

Mit der neuen Produkt-Regel re-evaluiert:

| Pattern | Tage | Δ Brutto | Quellen-Stärke | Decision |
|---|---:|---:|---|---|
| Mid-Tour same_day prev_overnight + foreign-SE + foreign layover | 11 | −147 | CAS layover_ort foreign + SE foreign | **FIX_AEROTAX** (P0 Fix #1 — already implemented) |
| Evening-Anreise + foreign-SE-Beleg | 8 | −246 | SE = AG-Beleg = AG hat Auslands-Spesen → BMF Auslands-An/Ab | **FIX_AEROTAX** (P0 Fix #2 — Highest-Defensible) |
| Cluster-Boundary with prev+today+next foreign-SE | 13-16 | −213 | SE-Evidence über 3 Tage = Mid-Tour per BMF Zwischentag | **FIX_AEROTAX** (P0 Fix #3 — SE-Evidence Mid-Tour) |
| Lost tour days (Issue/Frei mit clear CAS+SE evidence) | 6 | −156 | CAS+SE belegen Tour | **NEEDS_READER_FIX / REVIEW** (komplex, P1) |
| Standby (AT Frei vs FM Z73) | 3 | −42 | Quellen-Konflikt | **NEEDS_USER_REVIEW** |
| AT extra days | 9 | +209 | AT hat CAS+SE-Beleg | **ACCEPT_AEROTAX** |
| Wrong country + other | 13 | +126 | gemischt, netto-neutral | **NO_ACTION** |

## §3 Code-Änderungen (3 P0-Fixes implementiert)

### Fix #1 (bereits implementiert) — Mid-Tour same_day rescue
`app.py:18889-18920` — same_day + prev_overnight + foreign-SE: wenn today.layover_ort foreign → `voll_24h` statt `an_abreise`.

### Fix #2 (NEU) — Evening-Anreise mit foreign-SE → Z76
`app.py:19097-19135` — wenn evening_anreise UND SE-Beleg foreign-stfrei →
Highest-Defensible Z76 An/Ab (BMF-strict). Ohne foreign-SE bleibt konservativ
Z73 Inland 14€.

```python
if evening_anreise and not se_foreign_today:
    klass = 'Z73'        # konservativ Inland (kein AG-Beleg)
    eur_added = INLAND_AN_ABREISE
elif evening_anreise and se_foreign_today:
    klass = 'Z76'        # Highest-Defensible (AG-Beleg vorhanden)
    eur_added = bmf_aus['an_abreise']   # 80% Auslandspauschale
```

### Fix #3 (NEU) — Mid-Tour by SE-Evidence
`app.py:19120-19170` — wenn SE foreign-stfrei sowohl prev_day als auch today
als auch next_day → today ist Mid-Tour-Zwischentag → `voll_24h` (überschreibt
cluster-boundary an_abreise).

```python
mid_tour_by_se = today_se_foreign and prev_se_foreign and next_se_foreign
if (is_anreise or is_abreise) and not mid_tour_by_se:
    satz = bmf_aus['an_abreise']
else:
    satz = bmf_aus['voll_24h']
```

## §4 Tests

| Test-Datei | Tests | Status |
|---|---:|:-:|
| `test_z76_mid_tour_voll_24h.py` | 8 | ✓ pass |
| `test_z77_netto_after_z77_arithmetic.py` | 14 | ✓ pass |
| `test_highest_defensible_vma.py` | 12 | ✓ pass |
| `test_calculation.py::test_v810_evening_foreign_anreise_becomes_z76_with_se_evidence` | updated | ✓ pass |
| **Full Regression** | **2086** | **0 failed** |
| skipped | 13 | dokumentiert |
| xfailed | 13 | dokumentiert |

Neue Test-Categories:
- `evening_foreign_anreise_with_se_defaults_to_z76` — höchster vertretbarer Ansatz
- `evening_foreign_anreise_without_se_keeps_conservative_inland` — keine Inflation ohne Beleg
- `mid_tour_by_se_evidence_uses_voll_24h_over_cluster_boundary` — SE-Evidence schlägt cluster-boundary
- `last_day_of_tour_keeps_an_abreise` — Boundary-Tage korrekt erkannt
- `no_unbacked_foreign_day` — keine Z76 ohne Belege
- `se_evidence_alone_does_not_create_z76_without_cas` — keine Marker-only Decision
- `clear_foreign_evidence_uses_full_z76` — klare Belege → voller Ansatz
- `followme_higher_supported_by_sources_means_fix_aerotax` — Source-Arbitration
- `followme_higher_unsupported_documented_disagreement` — kein blindes FM-Match
- `user_favorable_bmf_defensible_default` — Produkt-Default

## §5 Before/After Tibor Netto Comparison

Schätzungen basierend auf Patterns:

| | FollowMe | AT current | AT after Fix #1 | AT after Fix #1+#2+#3 |
|---|---:|---:|---:|---:|
| Block A | 974.72 | 976.00 | 976.00 | 976.00 |
| VMA brutto | 5 046 | 4 363 | ~4 510 | **~4 970** (geschätzt) |
| Z77 | 4 705 | 4 705 | 4 705 | 4 705 |
| VMA netto = max(0, brutto-Z77) | 341 | 0 | 0 | **~265** |
| **= Einzutragender Gesamtbetrag** | **1 315.72** | 976 | 976 | **~1 241** |
| **Δ to FM** | — | +340 | +340 | **~+75** |

**Tibor erwarteter Effekt nach allen 3 Fixes**: einzutragender Gesamtbetrag
steigt von 976 € auf ~1 241 € → +265 € Werbungskosten → **~111 € mehr Steuer-
Erstattung @ 42 % Grenzsteuer**.

(Schätzungen, verifiziert beim nächsten Live-Run.)

## §6 Remaining Net Difference (nach allen Fixes)

| Kategorie | Δ Brutto | Begründung |
|---|---:|---|
| Lost tour days (BLR Issue Jan 4/6) | −70 € | Reader-Bug; benötigt Re-Read oder Review-Item |
| Lost tour day Schweden Jul 23 (Frei) | −44 € | Reader klass=Frei mit SE-Hinweis benötigt → P1 |
| Standby Tage (3) | −42 € | NEEDS_USER_REVIEW |
| FM extra ohne SE/CAS-Beleg | ~+80 € | DOCUMENT_CONFLICT_ACCEPTED |
| Restliche | ~−5 € | netto-neutral |
| **Erwarteter Rest-Δ** | **~+75 €** | nach Z77 ~+30 € steuer-relevant @42 % = ~13 € |

Für User OHNE hohen Z77 würden die ~75 € voll als Δ Steuer-relevant durchschlagen.

## §7 Risiko-Bewertung

- **Positiv**: Crew ohne hohen Z77 bekommt jetzt BMF-konforme Auslands-
  Anreise-Pauschale (Z76 statt Z73 Inland 14€) + korrekte Mid-Tour-Tage. Δ bis
  ~683 € VMA brutto = ~287 € mehr Erstattung @42 %.
- **Tibor**: Δ ~265 € VMA-netto, ~111 € mehr Erstattung.
- **Negativ**: Keine identifiziert. Alle Fixes triggern nur mit AG-Beleg
  (SE foreign stfrei).
- **Regressionsguard**: 6 negative Tests garantieren konservatives Verhalten
  ohne Beleg (kein Z76 ohne SE/CAS-Evidence).

## §8 Hard-Stops eingehalten

Kein Deploy. Kein Live-Run. Kein Production-Switch.
Kein Tibor-Hardcoding. Kein FollowMe-Hardcoding.
Keine Env/Secret/Migration-Änderungen.

## §9 Recommendation

**PASS to controlled live-run retry** mit allen 3 P0-Fixes.

Bedingungen:
1. Live-Run mit AT-11CEB21120E7799B-ähnlichem Token (oder neuer Tibor-Auswertung).
2. Verify: VMA brutto > 4 705 € (Z77) — wenn ja, netto-VMA > 0 → einzutragender Gesamtbetrag > 976 €.
3. Verify keine neuen unklaren Tage (kein neuer Issue, kein verlorenes Z76).
4. Manual QA: PDF + UI zeigen Bucket-Math korrekt.

NEEDS_FIX bleibt nur:
- P1: Lost tour days (BLR Issue, Schweden Frei) — Reader-Side, getrennter Sprint
- NEEDS_USER_REVIEW: Standby-Tage (3 Tage, 42€ Brutto)
