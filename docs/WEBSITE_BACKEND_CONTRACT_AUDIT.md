# Website ↔ Backend Contract Audit

Stand: 2026-05-20 (MegaR Phase 1).

Quelle:
- Frontend: `/Users/miguelschumann/Desktop/site/index.html` (line 3345–3401: FormData build)
- Backend: `/Users/miguelschumann/Desktop/aerotax-backend/app.py` Z1958 `/api/process`

## §1 Frontend → Backend Contract (Felder)

| # | Frontend JS-Var | DOM id | FormData key | Backend variable | Required? | Validation | Used in calculation? | Used in PDF? | Used in result_data? | Risk if missing | Test |
|---|---|---|---|---|:-:|---|:-:|:-:|:-:|---|:-:|
| 1 | `(vn+' '+nn).trim()` | #vn + #nn | `name` | `form['name']` | ✓ | string ≤80 | nein | ✓ | ✓ | Default 'Flugbegleiter' | ✓ |
| 2 | `vn` | #vn | `vorname` | `form['vorname']` | ✓ | string | nein | ✓ | ✓ | default '' | ✓ |
| 3 | `nn` | #nn | `nachname` | `form['nachname']` | ✓ | string | nein | ✓ | ✓ | default '' | ✓ |
| 4 | `km` (parseFloat input) | #km | `km` | `form['km']` capped 0-500 | ✓ | int 1-300 (HTML), float 0-500 (BE) | ✓ Fahrtkosten | ✓ | ✓ | 0 → Fahrtkosten 0 (kein Crash) | ✓ |
| 5 | `base` | #base (select) | `base` | `form['base']` | **HARD-REQUIRED** | non-empty | ✓ ALLE Module | ✓ | ✓ | **HTTP 400 UPLOAD_MISSING_REQUIRED** ✓ | ✓ |
| 6 | `window._selectedYear` | #yc-2023/24/25 | `year` | `form['year']` clamped 2023-2026 | ✓ | int 2023-2026 | ✓ BMF + Engine | ✓ | ✓ | Default 2025 | ✓ |
| 7 | `anreiseFinal` (CSV) | hidden #anreise + checkboxes | `anreise` | `form['anreise']` | ✓ | "auto"/"oepnv"/"shuttle"/"fahrrad" CSV | ✓ Fahrtkosten-Modes | ✓ | ✓ | Default 'auto' | ✓ |
| 8 | `fahrzeug` | hidden #fahrzeug | `fahrzeug` | `form['fahrzeug']` | ✓ | string | ✓ Fahrtkosten-Tarif | ✓ | ✓ | Default 'verbrenner' | ✓ |
| 9 | `oepnv_kosten` | input | `oepnv_kosten` | `form['oepnv_kosten']` capped 0-10k | optional | float | ✓ Werbungskosten | ✓ | ✓ | Default 0 | ✓ |
| 10 | `jobticket` | input | `jobticket` | `form['jobticket']` | optional | 'ja'/'nein' | ✓ Crosscheck LSB-Z17 | ✓ | ✓ | Default 'nein' | ✓ |
| 11 | `shuttle_kosten` | input | `shuttle_kosten` | `form['shuttle_kosten']` capped 0-10k | optional | float | ✓ Werbungskosten | ✓ | ✓ | Default 0 | ✓ |
| 12 | `anfahrt_min_in` | #anfahrt-min | `anfahrt_min` | `form['anfahrt_min']` capped 0-180 | optional | int | ✓ Plausi | ✓ | ✓ | Default 0 | ✓ |
| 13 | `ups.lsb` | #f-lsb | `lsb` (multipart) | `request.files.getlist('lsb')` | **YES** | min 1 PDF | ✓ Brutto/Z17 | ✓ | ✓ | HTTP 400 | ✓ |
| 14 | `ups.se` | #f-se | `se` (multipart) | `request.files.getlist('se')` | **YES** | min 1 PDF | ✓ Spesen | ✓ | ✓ | HTTP 400 | ✓ |
| 15 | `ups.cas` | #f-cas | `cas` (multipart) | `request.files.getlist('cas')` | **YES** | min 1 PDF | ✓ Tour/Marker | ✓ | ✓ | HTTP 400 | ✓ |
| 16 | optional Belege | 28× #f-* | various | `request.files` | optional | PDF | ✓ Werbungskosten | ✓ | ✓ | Skip | ✓ |
| 17 | `_freeRetryToken` | – | `free_retry_token` | `request.form.get('free_retry_token')` | optional | UUID | – | – | – | Payment-Required wenn fehlend | ✓ |
| 18 | `sendRef` | – | `ref` | `request.form.get('ref')` | optional (oder Token) | string | – | – | – | File-Lookup-Fallback | ✓ |
| 19 | `_paymentIntentId` | – | `payment_intent_id` | `request.form.get('payment_intent_id')` | conditional | string | – | – | – | Webhook-Race-Schutz | ✓ |
| 20 | `promo_code` | #promo-in | `promo_code` | `request.form.get('promo_code')` | optional | uppercase | – | – | – | Payment-Bypass | ✓ |

## §2 Gap-Analysis

### Frontend sendet, Backend liest nicht (= Datenverlust):

KEINE Gaps gefunden — alle 20 Frontend-Felder werden im Backend korrekt gelesen.

### Backend liest, Frontend sendet nicht (= Default-Fallback):

| Backend variable | Default | Risk |
|---|---|---|
| `ausfallzeit_monate` | 0 | gering (Optional-Feature für Mutterschutz/Krank); kein Frontend-Input — bewusst |
| `name` | 'Flugbegleiter' | gering (FE sendet always) |
| `dp` (Flugstundenübersicht) | – | per Master removed — FE sendet nicht mehr; Backend rejected bei direct dp-upload |
| `einsatz` | – | per CLAUDE.md aus Produkt entfernt — Backend hat noch Legacy-Pfad (TODO §3) |

## §3 Document Type Routing

| Doc-Type | Frontend FormData key | Backend handling | Status |
|---|---|---|---|
| **lohnsteuerbescheinigung** | `lsb` | active reader `_sonnet_read_lsb_v2` | ✓ |
| **streckeneinsatz** | `se` | active reader `_sonnet_read_se_structured` | ✓ |
| **dienstplan_cas** | `cas` | active reader `_sonnet_read_cas_structured` + V2 | ✓ |
| **legacy_ignored_flight_hours_summary** | (nicht im FE) | Audit-Label nur, hartstop wenn ohne CAS hochgeladen | ✓ (per FinalFix) |
| Belege (28 Typen) | `tel/gew/arb/fort/konz/lapt/fach/reini/bewer/stb/bu/haft/kv/rv/leb/haus/arzt/zahn/medi/pfle/under/kata/spen/part/kind/hand/haed` | `parse_optionale_belege` | ✓ |

**Flugstundenübersicht** ist im Frontend nicht mehr Pflicht-Karte. Wenn ein User versehentlich eine hochlädt (z.B. in `lsb` oder `cas`-Slot), wird sie per `classify_uploaded_pdf_doc_type` als `legacy_ignored_flight_hours_summary` erkannt und der CAS-Reader refuset sie. ✓

## §4 Critical Required Fields

| Field | Frontend Pflicht | Backend Hard-Validation | Behaviour wenn fehlt |
|---|:-:|:-:|---|
| `base` (Homebase) | ✓ Pflicht-UI | ✓ HTTP 400 | „Pflichtfeld Homebase fehlt" |
| `lsb` | ✓ Upload-Card | ✓ HTTP 400 | „Brauchst LSB+SE+CAS" |
| `se` | ✓ Upload-Card | ✓ HTTP 400 | gleich |
| `cas` | ✓ Upload-Card | ✓ HTTP 400 | gleich |
| `dp` direkt ohne CAS | – | ✓ HTTP 400 | „Flugstundenübersicht wird im neuen Ablauf nicht mehr benötigt" |
| `year` | ✓ Pflicht-Card | optional (Default 2025) | klemmt auf 2023-2026 |
| `name`/`vorname`/`nachname` | ✓ UI-Pflicht | nicht-zwingend | Default 'Flugbegleiter' (PDF-Anzeige) |

## §5 Validation Limits (Server-side Caps)

| Field | Min | Max | Source |
|---|---:|---:|---|
| `year` | 2023 | 2026 | Z1979 `year_input = max(2023, min(2026, year_input))` |
| `km` | 0 | 500 | Z1983 |
| `anfahrt_min` | 0 | 180 | Z1985 |
| `oepnv_kosten` | 0 | 10000 | Z1987 |
| `shuttle_kosten` | 0 | 10000 | Z1989 |
| `ausfallzeit_monate` | 0 | 12 | Z1992 |

## §6 Risk-Bewertung

| Risiko | Kategorie | Aktueller Status |
|---|---|:---:|
| `base` hardcoded (FRA-bias) | HIGH | ✓ MITIGATED — Server validiert, kein FRA-Default |
| Year-Pollution (Steuersatz falsch) | MEDIUM | ✓ Clamping 2023-2026 |
| km-overflow | LOW | ✓ Cap 500 |
| Flugstundenübersicht im falschen Slot | LOW | ✓ `classify_uploaded_pdf_doc_type` + CAS-refuse |
| Free-Retry-Token Bypass | LOW | ✓ Server validiert via `_validate_free_retry_token` |
| Promo-Code-Brute-Force | LOW | ✓ Rate-Limit + Whitelist |
| Files in falschem Slot | MEDIUM | ✓ Doc-Type-Detection bei CAS-Reader |

## §7 Definition of Done für Phase 1

- [x] Alle 20 Frontend-Felder im Backend gelesen
- [x] Keine Gaps in Datenfluss
- [x] Dokumenttyp-Routing 3-Doc-Modell ✓
- [x] Server-side validation für jedes kritische Feld
- [x] Flugstunden-Fallback aus Frontend entfernt
- [x] Test-Suite vorhanden (siehe `tests/test_v11_upload_contract.py`, `tests/test_calculation.py::test_v11_*`)

Ergänzende Tests folgen in `tests/test_megar_phase1_website_backend_contract.py`.
