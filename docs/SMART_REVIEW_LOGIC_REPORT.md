# Smart Review Logic — Final Report

Stand: 2026-05-21.
Produkt-Regel: AeroTAX prüft erst alle Quellen selbst — fragt User nur bei
echten Geld-Hebeln + konkretem Kontext, niemals generisch.

## §1 Selbstprüfungs-Logik (Vor jeder Review-Frage)

| Schritt | Quelle | Action |
|---|---|---|
| 1 | CAS start_time + end_time | duty_duration_minutes ableiten |
| 2 | CAS overnight_after_day + layover_ort | Tour-Kontext erkennen |
| 3 | SE stfrei_ort + stfrei_inland | Foreign-/Inland-Beleg prüfen |
| 4 | LSB Z17 / Z77 | AG-Zuschüsse + steuerfreie Spesen |
| 5 | User Form: commute_minutes (`anfahrt_min`) | Plausible Fahrtzeit hinzurechnen |
| 6 | Vortag/Folgetag-Kontext | Tour-Sequenz nutzen |
| 7 | Homebase + Routing | An-/Abreise/Mid-Tour bestimmen |

→ Erst wenn nach Schritt 1-7 noch eine **knappe oder geldrelevante** Unsicherheit
bleibt: Review-Item generieren.

## §2 Wann entsteht ein Review-Item?

### Near-8h Review (IMPLEMENTED)

| total Minuten (duty+commute) | Action |
|---|---|
| ≥ 480 (≥ 8:00) | auto Z72, **kein Review** |
| 420-479 (7:00-7:59) | **Review-Item** mit konkreter Minutenzahl |
| < 420 (< 7:00) | kein Review (zu klein für Hebel) |

**Voraussetzung**: duty_known=True UND nicht overnight/cluster/foreign-SE.

### Lost-tour-day Review (NOT_IMPLEMENTED yet — P2)

Geplante Evidence-Scoring-Logik:
- SE foreign stfrei (current day) → +3 Punkte
- Vortag layover foreign → +2
- Folgetag layover foreign → +2
- CAS marker/day-suffix → +1
- FollowMe-Referenz zählt mit → +1
- CAS klar OFF/Frei → −5

Score ≥ 4 → Review-Item; < 4 → silent Frei.

### Standby Review (NOT_IMPLEMENTED yet — P2)

Geplant: nur Review wenn:
- CAS marker `RES/SBY/SB` UND
- weder Vortag noch Folgetag foreign-Tour UND
- keine Uhrzeit/Ort-Info im CAS

## §3 Neue Chat-Copy (Beispiele)

### Near-8h (IMPLEMENTIERT)

**Mit Fahrtzeit angegeben (`commute_minutes > 0`)**:
> Ich komme für den 21.04. auf 7:55 Std. Abwesenheit aus dem Dienstplan
> (inkl. 30 min Fahrtzeit je Richtung). Wenn deine tatsächliche Hin- und
> Rückfahrt länger war, könntest du über 8 Stunden liegen. Ab mehr als 8
> Stunden kann eine Verpflegungspauschale (14 €) angesetzt werden. Warst du
> an diesem Tag inklusive Hin- und Rückweg länger als 8 Stunden unterwegs?

**Ohne Fahrtzeit angegeben**:
> Ich komme für den 21.04. auf 7:55 Std. Abwesenheit aus dem Dienstplan.
> Wenn deine tatsächliche Hin- und Rückfahrt dazu kommt, könntest du über
> 8 Stunden liegen. Ab mehr als 8 Stunden kann eine Verpflegungspauschale
> (14 €) angesetzt werden. Warst du an diesem Tag inklusive Hin- und
> Rückweg länger als 8 Stunden unterwegs?

**Antwortoptionen**:
- Ja, über 8 Stunden
- Nein, unter 8 Stunden
- Ich gebe Uhrzeiten ein
- Ich weiß es nicht

### Lost-Tour-Day (PLANNED-Copy für nächste Iteration)

> Der 23.07. sieht im Dienstplan unklar/frei aus, aber die Streckeneinsatz-
> daten und die Tage davor/danach deuten auf eine Auslandstour hin. Warst
> du an diesem Tag noch im Layover/auf Tour?

### Standby (PLANNED-Copy)

> Am 21.04. steht Bereitschaft/Reserve im Dienstplan. Ich kann nicht sicher
> erkennen, ob das zu Hause, am Flughafen oder im Hotel/Layover war. Wo
> warst du während dieser Bereitschaft?

## §4 Implementierte Änderungen

### Backend `app.py`

1. **Variable `near_8h_review_candidates`** am Function-Start von
   `_deterministic_classify_v7` deklariert (Python-scope-rule).
2. **Capture im Office-Branch** (`app.py:18950` area): wenn `duty_known
   AND 420 ≤ total < 480 AND not overnight AND not in_cluster`, wird ein
   Eintrag mit `total_min_known`, `minutes_to_8h`, `commute_minutes_input`
   in die Candidate-Liste appended.
3. **Surface in classifier result** (`app.py:20455` area):
   `'near_8h_review_candidates': near_8h_review_candidates`.
4. **`_build_review_items` erweitert** (`app.py:22173` area): neue Schleife
   über near_8h_review_candidates erzeugt Review-Items vom Typ `near_8h_review`
   mit kontextueller Frage (echte Std/Min + Money-Hinweis 14€).
5. **Sortierung nach money_impact_estimate desc** war bereits vorhanden,
   greift jetzt auch für near_8h-Items.

### Tests (`tests/test_near_8h_review_helpful.py` NEU)

10 Tests:
- `test_clear_over_8h_no_review_auto_z72` — klar ≥8h → kein Review
- `test_clear_under_8h_no_review` — weit unter 8h → kein Review
- `test_near_8h_creates_review_candidate` — 420-479 → Candidate
- `test_near_8h_question_mentions_actual_minutes` — Frage hat „7:55"
- `test_near_8h_question_mentions_money_effect` — Frage hat „Verpflegungspauschale" + „14"
- `test_near_8h_options_include_yes_no_time_unsure`
- `test_near_8h_review_item_has_source_type_cas`
- `test_review_items_prioritized_by_money_effect`
- `test_no_generic_what_was_this_day_question` — Statik-Audit
- `test_no_cas_upload_prompt_when_cas_present` — Statik-Audit

## §5 Beispiel: 7:55h-Frage in vollem Wortlaut

Eingabe (CAS):
- 21.04.2025: start_time=09:00, end_time=15:55, duty_duration_minutes=415
- Form: anfahrt_min=30

Berechnung:
- total = 415 (duty) + 60 (2× commute) = 475 min = **7:55 Std.**
- 475 ∈ [420, 480) → Near-8h-Review-Candidate

Generierte Frage:
> Ich komme für den 2025-04-21 auf 7:55 Std. Abwesenheit aus dem Dienstplan
> (inkl. 30min Fahrtzeit je Richtung). Wenn deine tatsächliche Hin- und
> Rückfahrt länger war, könntest du über 8 Stunden liegen. Ab mehr als 8
> Stunden kann eine Verpflegungspauschale (14 €) angesetzt werden. Warst du
> an diesem Tag inklusive Hin- und Rückweg länger als 8 Stunden unterwegs?

Wenn User "Ja" antwortet:
- Override `over_8h=True` per `review-answer` Endpunkt
- Recompute setzt Z72 (14€)
- source_type=`user` (Stern-Markierung im PDF)
- Audit-Note: "Abwesenheit >8h vom Nutzer bestätigt."

## §6 Wird Nutzer nur bei echten Geld-Hebeln gefragt?

**Ja** — alle Review-Items haben `money_impact_estimate` ≥ Geld-Schwelle:
- Near-8h: 14€ (Z72 Inland)
- Office/Training-time-missing: 14€
- Unknown-Marker: variabel je Marker-Bedeutung
- Sort: descending by money_impact_estimate → high-value items zuerst

**Nicht-relevante Klärungen werden silent gehandhabt**:
- Klar über 8h → auto Z72 (no question)
- Klar unter 7h → keine Frage (zu kleiner Hebel)
- Klar passive Marker (`ORTSTAG`/`FRS`/`LMN_AS`/`LMN_CR`/`FRD`) → silent-skip
- CAS layover=foreign + SE foreign → automatisch Z76 (P0-Fixes #1+#2+#3)
- CAS unklar aber Vortag+Folgetag eindeutig Tour → Tour-Kontext nutzt Continuity-Rules

## §7 Outstanding (P2 — vor Public Launch)

| Item | Status |
|---|---|
| Near-8h Review | ✅ implementiert + getestet |
| Lost-tour-day Evidence-Scoring | ❌ designed, not implemented |
| Standby contextual Review | ❌ designed, not implemented |
| Frontend: kontextuelle Anzeige der near_8h-Items in Chat | ❌ inherits from existing review-system |

## §8 Status

- **Backend**: 2113 pytest grün (2103 + 10 neue near-8h Tests)
- **Frontend**: keine Änderung in dieser Iteration (`near_8h_review` Items
  laufen durch bestehende `_review_items` → Chat-Review-System)
- **Local-only**: kein Deploy, kein Live-Run, kein Production-Switch

## §9 Recommendation

**PASS for local-test + P0 deploy** wenn:
1. Backend Cloud-Run Re-Deploy (P0 #1+#2+#3 + Smart-Review + source-labels)
2. Frontend unverändert (Markers laufen über bestehendes Review-Chat-System)
3. Live-Run mit Test-Token zeigt Near-8h-Frage mit echter Minutenzahl

**Hard-Stops respektiert**: Kein Deploy. Kein Live-Run. Kein Production-Switch.
