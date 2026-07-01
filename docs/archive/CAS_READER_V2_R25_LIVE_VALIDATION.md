# CAS Reader V2 — R15 Live-Validation Report

**Stand:** 2026-05-27T16:08:25.727078
**Branch:** `main`
**Tibor-Dir:** `/Users/miguelschumann/Desktop/Tibor/2025`
**Skip:** v1=True, v2=False

## 1. Setup

| Run | Flag | Wallclock | OK | _v2_active gesehen |
| --- | --- | --- | --- | --- |
| V1  | AEROTAX_CAS_READER_V2 not set | Nones | None | None |
| V2  | AEROTAX_CAS_READER_V2=1       | 1339.9s | True | False |

Errors V1: `—`
Errors V2: `—`

## 2. KPI Vergleich V1 vs V2

| KPI | V1 | V2 | Diff |
| --- | --- | --- | --- |
| z72_tage | None | 4 | None |
| z72_eur | None | 56.0 | None |
| z73_tage | None | 9 | None |
| z73_eur | None | 126.0 | None |
| z74_tage | None | 1 | None |
| z74_eur | None | 28.0 | None |
| z76_tage | None | 117 | None |
| z76_eur | None | 5071.0 | None |
| fahrtage | None | 39 | None |
| arbeitstage | None | 122 | None |
| reinigungstage | None | 122 | None |
| hotel_naechte | None | 83 | None |
| trinkgeld_eur | None | None | None |
| gesamt_eur | None | None | None |

### Normalized-Tours-Audit (Parallelpfad)
V1: `None`
V2: `{'fahrtage': None, 'arbeitstage': None, 'hotel_naechte': None, 'reinigungstage': None, 'z72': None, 'z73': None, 'z74': None, 'z76': None}`

## 3. Bekannte Problemfaelle (V2-Run)

- **2025-01-06** — BLR Heimkehr — X darf NICHT als Frei verloren gehen
  - legacy: {'klass': 'Z76', 'marker': 'X -09:21 FRA', 'routing': 'BLR', 'layover_ort': None, 'overnight': None, 'eur': 28.0, 'reason_counted': None, 'why_suspicious': None}
  - normalized: None
- **2025-01-04** — BLR Mid-Tour — X innerhalb Layover
  - legacy: {'klass': 'Z76', 'marker': 'X BLR', 'routing': 'BLR', 'layover_ort': None, 'overnight': None, 'eur': 42.0, 'reason_counted': None, 'why_suspicious': None}
  - normalized: None
- **2025-01-05** — BLR Mid-Tour — X innerhalb Layover
  - legacy: {'klass': 'Z76', 'marker': '755 LH755-1 BLR 23:28-', 'routing': 'BLR', 'layover_ort': None, 'overnight': None, 'eur': 42.0, 'reason_counted': None, 'why_suspicious': None}
  - normalized: None
- **2025-04-08** — Pattern A residual — target_iata-Risiko
  - legacy: {'klass': 'Z73', 'marker': '90064', 'routing': 'FRA', 'layover_ort': None, 'overnight': None, 'eur': 14.0, 'reason_counted': None, 'why_suspicious': None}
  - normalized: None
- **2025-10-05** — Pattern A residual — target_iata-Risiko
  - legacy: {'klass': 'Z73', 'marker': '18776', 'routing': 'FRA', 'layover_ort': None, 'overnight': None, 'eur': 14.0, 'reason_counted': None, 'why_suspicious': None}
  - normalized: None

## 4. Audit-Auszug

Volle Daten in `R15_VALIDATION_OUTPUT.json` (Roh-JSON beider Runs).

## 5. Entscheidung

**PENDING — bitte KPI-Diff + critical days pruefen und manuell PASS_FOR_STAGING oder NEEDS_FIX setzen.**

## 6. Wenn NEEDS_FIX

Konkrete Abweichungen werden hier nach Analyse manuell ergaenzt (Datum, erwarteter Wert, tatsaechlicher Wert, vermutete Ursache, Funktion, Fix-Vorschlag).
