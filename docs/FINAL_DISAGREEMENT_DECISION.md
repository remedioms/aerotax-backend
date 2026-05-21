# FINAL Disagreement Decision — 05-17, 06-17, 06-18

Stand: 2026-05-20.

Eingabe: CAS Detail (V2 Fixture), SE-Stempel, Golden Tour-Definition.

## §1 2025-05-17 — USA-Tour-Day-4

### CAS-Detail (Reader-Facts)

```
marker_raw            : 'OFF'
activity_type         : 'frei'
routing               : []
layover_ort           : ''
overnight_after_day   : False
starts_at_homebase    : False
ends_at_homebase      : False
duty_duration_minutes : 0
start_time            : ''
sources               : ['CAS']
```

### SE-Stempel
```
count: 0  (KEIN SE-Eintrag fuer 05-17)
```

### Golden-Behauptung
```
klass         : 'Z76'
land          : 'Vereinigte Staaten von Amerika (USA)'
betrag        : 40
dauer_h       : 10.48
tour_num      : 20
tour_size     : 4 (05-14 bis 05-17)
position_in_tour: 4 (=Abreise)
is_abreise    : True
```

### Tour-Context (CAS)

| Datum | CAS marker | activity | routing | layover | overnight |
|---|---|---|---|---|---|
| 05-15 | `X TLV` | frei | – | **TLV** | True |
| 05-16 | `112337 PU` | same_day | – | – | False |
| **05-17** | `OFF` | frei | – | – | False |
| 05-18 | `OFF` | frei | – | – | False |
| 05-19 | `LMN_AS / LMN_CR1` | office | – | – | False |

**Kritischer Befund**: CAS zeigt **05-15 layover=TLV** (Tel Aviv-Israel), nicht USA. Golden's USA-Tour passt nicht zur CAS-Quelle für 05-15. CAS und Golden divergieren **bereits am Vortag**.

05-17 CAS-Marker = `OFF` (explizit Frei). Kein routing, kein SE-Stempel.

### Entscheidung: **A — documented_reference_disagreement**

**Begründung**:
- CAS: keine Tour-Evidenz für 05-17 (OFF + kein routing + kein layover + kein duty)
- SE: keine Spesen
- Vortage zeigen **TLV** layover, nicht USA → Golden hat anderes Tour-Modell
- Per Master „CAS+SE sind Primärquelle": AeroTAX bleibt CAS-conform = Frei

**Konsequenz**: Test `test_tibor_no_known_missing_z76_layover_days[2025-05-17]` muss als documented_reference_disagreement markiert werden (xfail oder explizit skip mit reason).

---

## §2 2025-06-17 — Kroatien-Anreise

### CAS-Detail

```
marker_raw            : 'OFF'
activity_type         : 'frei'
routing               : ['FRA']
layover_ort           : ''
overnight_after_day   : False
duty_duration_minutes : 0
sources               : ['CAS']
```

### SE-Stempel
```
count: 0
```

### Golden-Behauptung
```
klass         : 'Z76'
land          : 'Kroatien'
betrag        : 31
dauer_h       : 8.33
tour_num      : 24
tour_size     : 2 (06-17 bis 06-18)
position_in_tour: 1 (=Anreise)
is_anreise    : True
```

### Tour-Context (CAS)

| Datum | CAS marker | activity | routing | overnight |
|---|---|---|---|---|
| 06-15 | `==` | frei | – | False |
| 06-16 | `==` | frei | – | False |
| **06-17** | `OFF` | frei | FRA | False |
| **06-18** | `OFF` | frei | FRA | False |
| 06-19 | `OFF` | frei | – | False |

Golden Tour 24: 06-17 nach Kroatien-Anreise + 06-18 Kroatien-Abreise. Tour-Size = 2 (sehr ungewöhnlich kurz).

**Kritischer Befund**: 5 Tage in Folge `==`/`OFF` Frei-Marker. KEIN routing nach Kroatien, KEIN layover, KEIN overnight. SE leer.

Vergleichend CAS-PDF (Dienstplan PUB_6) zeigte zuvor: 17.-20. Juni alle leer; erste echte Tour ist Sa 21 nach Athen (Tour 25).

→ CAS sagt **eindeutig Frei**. Golden behauptet eine Kroatien-Tour, die in CAS-Quelle nicht existiert.

### Entscheidung: **A — documented_reference_disagreement**

**Begründung**:
- 5 Tage in Folge Frei-Marker in CAS (06-15 bis 06-19)
- KEINE routing nach Kroatien
- KEIN SE-Stempel
- Echter Tour-Start ist erst 06-21 (nach Athen) → CAS-conform
- Per Master „CAS+SE sind Primärquelle": Pipeline bleibt Frei
- FollowMe.aero scheint hier eine Tour zu erfinden, die in Tibor's tatsächlichem Dienstplan nicht vorkommt

---

## §3 2025-06-18 — Kroatien-Abreise

Symmetrisch zu §2. Gleiche CAS-Frei-Evidenz, gleicher Befund.

### Entscheidung: **A — documented_reference_disagreement**

Pipeline bleibt Frei.

---

## §4 Master-Regel-Validierung

> Wenn CAS + SE keine Tour belegen → documented_reference_disagreement.

Alle 3 Tage erfüllen:
- CAS-Marker explizit `OFF`/`==` (Frei-Indikator)
- KEIN routing (außer evt. `[FRA]`-Defaultwert)
- KEIN layover_ort
- KEIN overnight_after_day=True
- KEIN duty_duration_minutes
- KEINE SE-Spesen

→ Alle 3 Tage sind **dokumentierte CAS-FollowMe-Disagreements**, keine Pipeline-Bugs.

---

## §5 KPI-Auswirkung

| KPI | Aktuell | Δ-Effekt wenn diese 3 Tage als Z76 gewertet würden |
|---|---:|---:|
| arbeitstage | 139 | +3 = 142 (weiter weg von 133) |
| hotel_naechte | 65 | +1 (06-17 wäre Anreise mit Hotel-Anspruch) |
| fahr_tage | 42 | +1 (06-17 Anreise) |
| z73 | 8 | +0 |
| z76_eur | 5484 | +102 (40+31+31) = 5586 |
| gesamt | 5694 | +102 = 5796 |

**Würde Golden Acceptance schlechter machen, nicht besser.** AeroTAX bleibt CAS-conform.

---

## §6 Operative Konsequenz

3 Acceptance-Tests müssen als documented_reference_disagreement markiert werden:

```python
@pytest.mark.xfail(reason='documented_reference_disagreement: CAS+SE haben keine '
                          'Tour-Evidenz, FollowMe scheint eine Phantom-Tour zu zaehlen. '
                          'Siehe docs/FINAL_DISAGREEMENT_DECISION.md.', strict=True)
def test_tibor_no_known_missing_z76_layover_days[2025-05-17]: ...
def test_tibor_no_known_missing_z76_layover_days[2025-06-17]: ...
def test_tibor_no_known_missing_z76_layover_days[2025-06-18]: ...
```

Alternative: Liste `KNOWN_CAS_FOLLOWME_DISAGREEMENT = {'2025-05-17', '2025-06-17', '2025-06-18'}` und Test-Parametrize um diese Tage filtern.

Pipeline bleibt unverändert. **0 Code-Änderungen für diese 3 Tage.**
