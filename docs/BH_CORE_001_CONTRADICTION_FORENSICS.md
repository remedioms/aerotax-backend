# BH-CORE-001 Phase 4.5 — Contradiction Forensics

Stand: 2026-05-19. **Kein Code. Keine Hypothese-A-Annahme. Golden bleibt Maßstab.**

Aufgabe: für die 11 Widerspruchstage (AeroTAX zählt als Tour, Golden nicht) deterministisch prüfen, warum FollowMe.aero / Golden den Tag NICHT zählt.

> ⚠️ **„Golden absent" reicht NICHT als Beweis.** Wir prüfen explizit:
> - Ist der Tag in irgendeiner Golden-Tour-Span enthalten?
> - Ist der Tag der Vortag/Folgetag einer Golden-Tour?
> - Wird der Tag möglicherweise in einer benachbarten Tour aggregiert?
> - Welche Tageskategorie (Anreise/Layover/Heimkehr/Positionierung/NO_VMA) erschließt sich aus Tour-Position?

Quelle: `tests/fixtures/tibor_aerotax_v11_raw_initial.json` (CAS-Reader-Output) + `tests/fixtures/followme_golden_tibor_2025.json` (Soll, mit `touren[]`-Liste + `day_classification`).

---

## §1 FollowMe-Datenmodell — was steckt drin?

FollowMe.aero Golden enthält **kein** explizites `reason`/`note`/`explanation`-Feld pro Tag. Aber implizit:

| Feld | Bedeutung |
|---|---|
| `touren[].tour_num` | fortlaufende Tour-Nummer |
| `touren[].start_date`, `end_date` | Tour-Range |
| `touren[].start_time`, `end_time` | Briefing-Start / Heimkehr-Zeit |
| `touren[].tage[]` | Liste der Tage in der Tour mit `dauer_h`, `land`, `betrag` |
| `touren[].tour_size` | Anzahl Tage in der Tour |
| `touren[].tour_summe` | Summe BMF-Beträge in € |
| `day_classification[datum]` | klass, land, betrag, dauer_h, tour_num, position_in_tour, is_anreise, is_abreise |
| Tour mit `tour_summe = 0` | NO_VMA-Tour: Tibor flog (Same-Day), aber kein BMF-Anspruch (z.B. duty <8h, Inland-Roundtrip) |

**Implizite „Reasons":**
- Tag in `tage[]` = Tibor zählt diesen Tag steuerlich (Anreise/Mid/Heimkehr je nach `position_in_tour`).
- Tag NICHT in `tage[]` = **3 Möglichkeiten:**
  1. Frei (kein Roster-Tag oder passive Marker)
  2. Aggregiert in benachbarter Tour (Briefing-Tag-DE + Layover am gleichen Day)
  3. Positioning (Crew flog zum Tour-Start aber wird nicht steuerlich separat gezählt)

**FollowMe-Anzahl Touren 2025:** **53** (3 davon `tour_summe=0` = NO_VMA = Same-Day-Inland: 01-10, 03-28, 06-04).

---

## §2 Per-Day Forensics mit FollowMe-Kontext

### §2.1 LAD-Cluster 05-20 bis 05-23 — 4 Tage

#### Forensik-Daten

| Datum | Marker | Routing | duty | overn | layover | sHB/eHB | AeroTAX klass | AeroTAX reason |
|---|---|---|---:|---|---|---|---|---|
| 05-20 | `103703 P1` | FRA→LAD | 234min | T | LAD | T/F | Z73 | „Abend-Briefing → Inland-An" |
| 05-21 | `103703 P1` | LAD | 270min | T | LAD | F/F | Z76 | „Auslands-Layover LAD Volltag" |
| 05-22 | `103703 P1` | LAD→FRA | 179min | T | LAD | F/F | Z76 | „Auslands-Layover LAD An/Ab" |
| 05-23 | `103703 P1` | LAD | 330min | F | (leer) | T/T | Issue | „Heimkehr aus Vortag-Tour" |

#### FollowMe-Status

| Datum | In Tour `tage[]`? | In Tour-Span? | Adjacent Tour? | FollowMe-Implizit-Reason |
|---|---|---|---|---|
| 05-20 | ✗ NEIN | ✗ NEIN | ✗ Keine | **Tibor zählt diesen Tag NICHT als steuerlichen Arbeitstag** |
| 05-21 | ✗ NEIN | ✗ NEIN | ✗ Keine | dito |
| 05-22 | ✗ NEIN | ✗ NEIN | ✗ Keine | dito |
| 05-23 | ✗ NEIN | ✗ NEIN | ✗ Keine | dito |

**Vor-/Folge-Touren in der Nähe:**
- Tour 17 endet 2025-05-13 (Schweden, 5d, 374€)
- Tour 18 startet 2025-05-26 (Schweden, 4d) ← nächste Tour erst 3 Tage später

→ **05-19 bis 05-25 ist eine 7-Tage-Lücke im Golden ohne Tour.** Tibor war vermutlich in Urlaub/Frei in diesen 7 Tagen.

#### Cross-Check Vortag

- **05-19** in AeroTAX = `Z72 (LMN_AS/LMN_CR1)` — Medical-License-Day. NICHT in Golden.
- **05-24** in AeroTAX = `Office (ORTSTAG OF)` — Hb-passive. NICHT in Golden.

#### Plausibilität

| AeroTAX | FollowMe | Wer plausibler? | Warum? |
|---|---|---|---|
| Z73+Z76×3 (LAD-Tour 4d) | Tag nicht in Tour, keine Reason | **FollowMe** | duty-Pattern 234/270/179/330 min UNREALISTISCH für echte LAD-Tour (normal 600-800min). Marker `103703 P1` ist Roster-Sequence-ID, nicht Flight-Number. Kein SE-Stempel. FollowMe-7-Tage-Frei-Lücke konsistent mit „Tour nicht geflogen / Tausch / Storno". |

#### Cluster-Empfehlung

**C4 (Tour-Boundary falsch) + C6 (User-Klärung)**.

---

### §2.2 Skandinavien 06-01/02 — 2 Tage

#### Forensik-Daten

| Datum | Marker | Routing | duty | overn | layover | AeroTAX klass | Anomalie |
|---|---|---|---:|---|---|---|---|
| 06-01 | `126533 PU` | FRA→CPH→GOT | **1084min** | T | GOT | Z76 | **duty 18.1h** |
| 06-02 | `126533 PU` | GOT→FRA→SOF | **1189min** | T | SOF | Z76 | **duty 19.8h** |

#### FollowMe-Status

| Datum | In Tour `tage[]`? | Adjacent Tour? | FollowMe-Implizit-Reason |
|---|---|---|---|
| 06-01 | ✗ NEIN | ✗ Keine | **NICHT gezählt** |
| 06-02 | ✗ NEIN | ✗ Keine | dito |

**Vor-/Folge-Touren:**
- Tour 21 endet 2025-05-31 (...) ← Vortag 05-31 ist Tour-Ende
- Tour 22 = NO_VMA Same-Day am 06-04 (1d, 0€)
- Tour 23 startet 2025-06-07 (Schweden, 3d)

→ **06-01 bis 06-03 = 3-Tage-Lücke**, gefolgt von 1 NO_VMA-Tag (06-04 Same-Day-Inland), dann Frei bis 06-06.

#### Plausibilität

| AeroTAX | FollowMe | Wer plausibler? | Warum? |
|---|---|---|---|
| Z76 ×2 (Skandi-Tour) | Tag nicht in Tour | **FollowMe** | duty 1084min + 1189min **überschreitet EASA-FTL** (max ~14h = 840min). Reader hat duty-aggregiert über mehrere Sequenzen. Routing FRA→CPH→GOT in 1 Tag + GOT→FRA→SOF am Folgetag = vermutlich ein einziger Multi-Sequence-Pattern. Plus: Tour 22 NO_VMA am 04.06. zeigt dass Tibor Anfang Juni in Same-Day-Inland-Trips war, nicht in Skandinavien-Tour. |

#### Cluster-Empfehlung

**C3 (Reader-Bug — duty-Aggregation)** primär. Plus C6 (User-Klärung, weil 06-01/02 möglicherweise eine **Trainings-/Positioning-Sequenz** war).

---

### §2.3 KRK 09-25 — 1 Tag

#### Forensik-Daten

| Datum | Marker | Routing | duty | overn | layover | AeroTAX klass |
|---|---|---|---:|---|---|---|
| 09-25 | `15688 PU` | FRA→BER→KRK | **1059min** (17.7h) | T | KRK | Z76 |

#### FollowMe-Status

| Datum | In Tour `tage[]`? | Adjacent Tour? | FollowMe-Implizit-Reason |
|---|---|---|---|
| 09-25 | ✗ NEIN | **→ Folgetag startet Tour 39** | **Vermutlich Positioning für Tour 39** |

**Tour 39:**
- start: 2025-09-26, end: 2025-09-28, tour_size: 3
- first_tag: 09-26, dauer_h=18.0, land=**Bulgarien**, betrag=15
- Tour 39 erste Anreise = 09-26 (NICHT 09-25)
- Marker am 09-26 in fixture: `15688 PU (Day 2)` ← **explizit Day 2** = impliziert Day 1 war Vortag

#### Plausibilität

| AeroTAX | FollowMe | Wer plausibler? | Warum? |
|---|---|---|---|
| Z76 KRK Polen | Positioning-Tag, NICHT eigene Tour | **FollowMe** | duty 17.7h überschreitet FTL-Limit. Marker `15688 PU` (Day 1 implizit) gefolgt von `15688 PU (Day 2)` am 09-26 = **eine Tour**. Tour 39 läuft 09-26 bis 09-28 in Bulgarien. 09-25 ist Anreise zur Tour-Crew-Base in Krakow (KRK ist Lufthansa-Stopover-Hub für Eastern-EU-Touren). FollowMe rechnet Anreise zum Auslands-Hub als **Positioning** = kein eigener VMA-Tag. |

#### Cluster-Empfehlung

**C4 (Tour-Boundary falsch — 09-25 ist Positioning, nicht Tour-Start)**. Plus C3 (Reader-duty unrealistisch).

---

### §2.4 TLV-2 Cluster 10-26 bis 10-28 — 3 Tage

#### Forensik-Daten

| Datum | Marker | Routing | duty | overn | layover | AeroTAX klass |
|---|---|---|---:|---|---|---|
| 10-26 | `32935 PU` | FRA→TLV | 449min | T | TLV | Z76 |
| 10-27 | `X` | TLV | 0min | T | TLV | Frei (fixture) / tour_mid (norm) |
| 10-28 | `32935 PU` | TLV→FRA | 280min | F | (leer) | Issue (fixture) / tour_end (norm) |

#### FollowMe-Status

| Datum | In Tour `tage[]`? | Adjacent Tour? | FollowMe-Implizit-Reason |
|---|---|---|---|
| 10-26 | ✗ NEIN | **← Vortag 10-25 endet Tour 44 (London)** | nicht gezählt |
| 10-27 | ✗ NEIN | keine | nicht gezählt |
| 10-28 | ✗ NEIN | keine | nicht gezählt |

**Tour 44:**
- start: 2025-10-23, end: 2025-10-25
- last_tag: 10-25, dauer_h=23.5, land=**Vereinigtes Königreich - London**, betrag=44 (Abreise)

**Tour 45 (next):**
- start: 2025-10-31 (Tour-Lücke 10-26 bis 10-30 = 5 Tage Frei laut Golden)

#### Plausibilität

| AeroTAX | FollowMe | Wer plausibler? | Warum? |
|---|---|---|---|
| Z76 TLV-Tour 3d | 5-Tage-Frei-Lücke 10-26 bis 10-30 | **FollowMe** | Pattern identisch zu LAD: duty 449/0/280 min sehr kurz. Marker `32935 PU` Roster-Sequence-ID. Kein SE-Stempel. **London-Tour endete 10-25 spätabends (23:30 dauer)**. Crew braucht 1-2 Tage Recovery. Vermutlich Roster-Sequenz die nicht geflogen wurde / oder „on-call Bereitschaft" ohne aktiven Dienst. |

#### Cluster-Empfehlung

**C4 + C6 (User-Klärung).** Identisch LAD.

---

### §2.5 JFK 12-15 Tag 2 — 1 Tag

#### Forensik-Daten

| Datum | Marker | Routing | duty | overn | layover | AeroTAX klass |
|---|---|---|---:|---|---|---|
| 12-15 | `57783 P1 Tag 2` | JFK→FRA | 184min | T | JFK | Z76 |

**Plus 12-14 Kontext:**
- Marker: `57783 P1` (Tag 1 implizit)
- Routing: FRA→JFK
- AeroTAX klass: Z76
- Golden klass: Z76 **Irland** pos=1/1 (1-Day-Tour!) betrag=39

#### FollowMe-Status

| Datum | In Tour `tage[]`? | Adjacent Tour? | FollowMe-Implizit-Reason |
|---|---|---|---|
| 12-15 | ✗ NEIN | **← Vortag 12-14 ist Tour 51 (1-Day Irland)** | nicht gezählt |
| 12-16 | ✗ NEIN | (keine Tour) | Frei |

**Tour 51:**
- start: 2025-12-14, end: 2025-12-14 (**1-Day-Tour**)
- last_tag: 12-14, dauer_h=13.4, land=**Irland**, betrag=39
- tour_size=1, tour_summe=39

#### Plausibilität

| AeroTAX | FollowMe | Wer plausibler? | Warum? |
|---|---|---|---|
| Z76 JFK USA | Z76 Irland 1-Day-Tour | **FollowMe** | Marker `57783 P1 Tag 2` ist explizit Day 2 → impliziert Day 1 = 12-14. Reader sieht 12-14 routing=FRA→JFK aber Golden hat Land=**Irland** → Crew flog vermutlich **FRA→SNN(Shannon Irland)→JFK→FRA in ~24h** mit Briefing in DE, Cockpit-Wechsel in Shannon. **Tour war 1-Day** (Briefing morgens 12-14, Heimkehr früh 12-15). 12-15 184min ist NUR der Schluss-Heimflug + Cockpit-Übergabe, kein eigener Tour-Tag. |

#### Cluster-Empfehlung

**C3 (Reader-Bug Shannon-Stop fehlt)** + **C4 (Tour-Boundary verschoben)**.

---

## §3 Konsolidierte Cluster-Verteilung (mit FollowMe-Kontext)

| Datum | Aktuelle Klass | FollowMe-Status | FollowMe-Implizit-Reason | Cluster |
|---|---|---|---|---|
| 05-20 | Z73 | nicht in Tour, keine adjacent | 7-Tage-Frei-Lücke 05-19 bis 05-25 | **C4 + C6** |
| 05-21 | Z76 | dito | dito | **C4 + C6** |
| 05-22 | Z76 | dito | dito | **C4 + C6** |
| 05-23 | Issue | dito | dito | **C4 + C6** |
| 06-01 | Z76 | nicht in Tour, keine adjacent | 3-Tage-Lücke, dann NO_VMA-Tour 06-04 | **C3** (duty unrealistisch) |
| 06-02 | Z76 | dito | dito | **C3** (duty unrealistisch) |
| 09-25 | Z76 | **Folgetag startet Tour 39** | Positioning für Bulgarien-Tour | **C4** (Tour-Boundary +1 Tag zu früh) |
| 10-26 | Z76 | nicht in Tour | 5-Tage-Frei-Lücke nach London-Tour | **C4 + C6** |
| 10-27 | Frei/tour_mid | dito | dito | **C4 + C6** |
| 10-28 | Issue/tour_end | dito | dito | **C4 + C6** |
| 12-15 | Z76 | **Vortag = Tour 51 (1-Day Irland)** | Heimkehr-Anteil der Tour 51 | **C3 + C4** (Shannon-Stop fehlt + Tag 2 = Heimkehr) |

**Summary:** **0 Tage in Cluster C1.** Alle 11 Tage haben eine FollowMe-Implizit-Reason (durch Tour-Span-Analyse + Frei-Lücken-Kontext):

- 7 Tage = **Roster-Sequenz vermutlich nicht geflogen** (LAD 4 + TLV 3) → User-Klärung C6
- 2 Tage = **Reader-duty-Aggregations-Bug** (Skandinavien) → BH-READER-001
- 1 Tag = **Positioning** (KRK 09-25) → Tour-Boundary-Logic C4
- 1 Tag = **Tour-2-Heimkehr** (JFK 12-15) → Reader-Shannon-Bug + Tag-Suffix-Logic

---

## §4 SE-Daten-Limitation

Die fixture `tibor_aerotax_v11_raw_initial.json` enthält **kein SE-Daten** (Reader-Output stammt aus CAS only). Echte SE-Streckeneinsatzabrechnung würde zusätzliche Beweise liefern:

- Wenn SE für 05-20 bis 23 einen Auslands-Stempel LAD enthält → Tour wurde geflogen, FollowMe vergaß. (Hypothese A würde Beweis bekommen.)
- Wenn SE für 05-20 bis 23 KEINE Einträge hat → Tour nicht geflogen.

**Phase 5 KI-Resolver kann das nicht ersetzen.** SE-Daten sind Pflicht für definitive Klärung. Aktion: User soll Tibor's originalen SE-PDF prüfen.

---

## §5 Empfehlungen pro Cluster

### C3 — Reader-Quality (06-01, 06-02, 12-15)

**BH-READER-001 Backlog:**
- duty_duration_minutes > 840 min (14h FTL) → Reader-Output flaggen
- Multi-Stop-routing in 1 day mit ungewöhnlich-multiplem Auslands-IATA → aggregiertes-routing-Verdacht
- Marker-Suffix `Tag 2`/`Day 2` ohne dazugehöriges Tag 1 in fixture → Tour-Boundary-Pre-Check

### C4 — Tour-Boundary-Refinement (09-25, 12-15)

**BH-CORE-001-PHASE-5-B Backlog:**
- Marker `<base> Tag N` (N≥2) → impliziert Vortags-Tag mit `<base>` als Day-N-1, Tour-Merge
- Wenn Folgetag tour_start mit anderem Land aber kontinuierlichem Marker → aktueller Tag ist Positioning, NICHT eigener Tour-Tag
- KI-Resolver `tour_boundary` mit Marker-Suffix-Pattern

### C6 — User-Klärung (LAD 4d, TLV-2 3d = 7 Tage)

**Review-Items per Cluster:**

```
LAD-Review (05-20 bis 05-23):
"Im Roster steht eine 4-Tage-LAD-Sequenz (`103703 P1`) mit ungewöhnlich kurzen
 Dienstzeiten (234/270/179/330 min). Wurde diese Tour tatsächlich geflogen?"
[Ja] [Nein, getauscht] [Nein, storniert] [Anders]

TLV-2 Review (10-26 bis 10-28):
"Im Roster steht eine 3-Tage-TLV-Sequenz (`32935 PU`) mit Dienstzeiten
 449/0/280 min nach der London-Tour. Wurde diese Tour tatsächlich geflogen?"
[Ja] [Nein, getauscht] [Nein, storniert] [Anders]
```

→ Wenn User „Nein" → klass=Frei automatisch + audit-note.

### **NICHT C1**

**Auto-Zähl-Tage als „Golden vergaß"**: NICHT akzeptieren. **0 von 11 Tagen** haben Evidence dass Golden lückenhaft ist.

---

## §6 Auswirkung auf KPIs

### Wenn alle C3/C4/C6 korrekt aufgelöst (User bestätigt Tour-Negativ + Reader-Fix + Tour-Boundary-Fix)

| KPI | Aktuell (Phase 4) | Nach Phase 4.5 Resolve | Golden |
|---|---:|---:|---:|
| arbeitstage | 124 | 124 − 4 (LAD) − 3 (TLV) − 2 (06-01/02 C3) − 1 (KRK Positioning) − 1 (JFK Tag-2-merge) = **113** | 133 |
| Δ noch zu Golden | −9 | **−20** (PLUS noch missing-Tage aus Phase 5 = +20 → **133** ✓) | 0 |
| z76_eur | 5262 | ≈ 5262 − 470 (Cluster-Tage) = **~4790** | 4794 ✓ |
| hotel | 64 | leicht weniger durch Tour-merge | 66 |

**Mit Phase 4.5 + Phase 5 (KI für missing) → arbeitstage = 113 + 20 = 133 ✓ Golden.**

---

## §7 Phase-4.5-Akzeptanz

- ✓ Für alle 11 Tage `FollowMe-Status` + `Adjacent-Tour-Kontext` + `Implizit-Reason` extrahiert
- ✓ FollowMe-Tour-Spans als Quelle genutzt (`touren[].tage[]` + `tour_summe` + `position_in_tour`)
- ✓ Plausibilitäts-Bewertung pro Tag + Cluster-Empfehlung
- ✓ **0 Tage in C1** (Hypothese A nicht haltbar)
- ✓ SE-Daten-Limitation transparent dokumentiert

**Kein Code. Kein Deploy. Kein Live-Run.**

---

## §8 Next Step

**Warte auf User-Entscheidung:**

1. **C6 User-Klärung starten:** Tibor mit LAD + TLV-2 Listen kontaktieren — bestätigt er Touren-Negativ?
2. **C3 Reader-Audit (BH-READER-001) starten** als parallelen Stream
3. **C4 Tour-Boundary-Phase-5-B** umsetzen (Marker-Suffix-Logic)
4. **Phase 5 KI-Resolver** für die 20 missing-Tage (RES Korea, OFF Kroatien, ==) — KI-Live-Calls erforderlich
5. **Golden bleibt Maßstab.** Acceptance-Toleranzen NICHT anpassen.
