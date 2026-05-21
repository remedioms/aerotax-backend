# German Tax Rules Validation

Stand: 2026-05-20 (Rel Phase 8).

## §1 Entfernungspauschale (§9 EStG)

| Regel | Implementation | Test |
|---|---|---|
| 0,30 €/km bis 20 km | hardcoded in Fahrtkosten-Berechnung | ✓ |
| 0,38 €/km ab 21. km (seit 2022) | hardcoded | ✓ |
| Einfache Entfernung | km × Fahrtage | ✓ |
| AG-Erstattung (Jobticket) abziehen | LSB Z17 / SE-Stempel | ✓ |
| ÖPNV-Kosten separat | form['oepnv_kosten'] | ✓ |
| Shuttle-Kosten separat | form['shuttle_kosten'] | ✓ |
| Kein doppelter Abzug | Topf-Trennung | ✓ |

## §2 VMA Inland (§4 Abs. 5 Nr. 5 EStG)

| Klass | Bedeutung | Satz (2025) | Trigger |
|---|---|---:|---|
| Z72 | Inland-Same-Day ≥8h | 14€ | starts_hb + ends_hb + duty≥480 |
| Z73 | Inland-An/Abreise (mit Übernachtung) | 14€ | tour_start oder tour_end mit foreign-Tour-Kontext |
| Z74 | Inland-Volltag 24h | 28€ | tour_mid mit foreign-tour aber SE-inland-Ort |

Pflicht-Regeln:
- ✓ Z72 nicht bei <8h
- ✓ Z72 nicht bei Office am Hb ohne Auswärtstätigkeit
- ✓ Z72 nicht bei Standby zuhause (außer activated)

## §3 VMA Ausland (BMF-Pauschalen 2025)

| Klass | Bedeutung | Trigger |
|---|---|---|
| Z76 voll_24h | Foreign-Tour-Mid mit overnight | role=tour_mid + foreign-layover |
| Z76 an_abreise | Foreign-Anreise/Abreise | role=tour_start oder tour_end |

Pflicht-Regeln:
- ✓ Z76 nur mit foreign-Layover + Tour-Evidenz
- ✓ BMF-Land/Satz aus Python-Tabelle (`bmf_data.BMF_AUSLAND_BY_YEAR`)
- ✓ SE-Ort als starke Quelle (validiert 92% Golden-Übereinstimmung)
- ✓ CAS-Layover als Fallback
- ✓ KI liefert NIE Beträge (Anti-Tax-Sanitizer)

## §4 Dreimonatsfrist

Status: **NICHT umgesetzt** in aktueller Pipeline. Risk-Document:
- 3-Monatsfrist gilt für längere Auswärtstätigkeit am selben Ort
- Für Flugpersonal mit wechselnden Destinationen meist NICHT relevant
- Für längere Schulung/Stationierung MUSS später ergänzt werden

**Decision**: NEEDS_DECISION / dokumentiert als Limitation. Aktuell low-risk weil Crew-Auswärtstätigkeit meist <90 Tage am selben Ort.

## §5 AG-Erstattung / Z77

| Pflicht | Status |
|---|:-:|
| SE-Z77-Spalte (steuerfreie AG-Spesen) abziehen | ✓ |
| LSB Z17 (Jobticket) berücksichtigen | ✓ |
| Topf-Trennung Z17/Z76/Z77 | ✓ |
| Keine doppelte Anrechnung | ✓ |

## §6 Weitere Werbungskosten

| Kategorie | Trigger | Test |
|---|---|---|
| Reinigungstage | = arbeitstage (Crew-Pauschale) | ✓ |
| Hotelnächte | nur foreign-overnight + Z73/Z74/Z76 | ✓ |
| Trinkgeld | Hotelnacht-basiert | ✓ |
| Optional Belege | nur mit Beleg-Upload | ✓ |

## §7 Disclaimer / Rechtshinweise

Per `docs/LEGAL_TEXT_RELEASE_AUDIT.md`:
- ✓ "Keine Steuerberatung"
- ✓ "Werbungskosten-Aufstellung"
- ✓ User-Prüf-Pflicht
- ✓ Keine Garantie-Aussagen

## §8 Definition of Done

- [x] Entfernungspauschale 20km-Grenze
- [x] VMA Inland Z72/Z73/Z74
- [x] VMA Ausland Z76
- [x] AG-Erstattung / Z77
- [x] BMF aus Python-Tabelle (KI NIE Beträge)
- [x] Disclaimer in PDF/UI
- [ ] Dreimonatsfrist (deferred, NEEDS_DECISION)
