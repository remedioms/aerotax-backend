# Optional-Receipts Classifier — Design-Doc

Stand: 2026-05-21. Status: **DESIGN ONLY** — Backend-Integration in eigenem Sprint.

## §1 Produkt-Regel

> AeroTAX rechnet optionale Belege nur ein, wenn Quelle, Betrag und Kategorie verlässlich erkannt sind. Optionale Belege werden niemals mit VMA/Z77 oder Fahrtkosten/Z17 vermischt — sie laufen in einem eigenen Topf, der separat im PDF ausgewiesen wird.

## §2 Per-Receipt Schema

Backend liefert pro hochgeladenem Beleg ein Klassifikator-Objekt:

```python
{
  'receipt_id': 'r_2025_01_15_uniform_invoice',   # stable hash
  'filename': 'uniform_2025_01.pdf',
  'ocr_text': '...',                              # raw OCR (intern, nicht im PDF)
  'amount_eur': 89.90,                            # float or None
  'date_iso': '2025-01-15',                       # YYYY-MM-DD or None
  'merchant': 'Uniform-Werkstatt GmbH',           # or None
  'category': 'reinigung_uniform',                # enum (siehe §3)
  'category_confidence': 0.92,                    # 0..1
  'amount_confidence': 0.96,
  'source_type': 'user_document',                 # source-label, NIE 'calculated'
  'included_in_total': True,                      # final decision
  'inclusion_reason': 'category=reinigung_uniform conf=0.92 amount=89.90 plausible',
  'needs_review': False,                          # only True if money-relevant + ambiguous
  'review_question': None,                        # populated only if needs_review=True
  'audit_notes': []                               # internal trail
}
```

## §3 Kategorie-Enum (grob)

```python
RECEIPT_CATEGORIES = {
    'arbeitsmittel':         'Crew-typische Arbeitsmittel (Trolley, Headset, Schuhe)',
    'weiterbildung':         'Sprachkurse, Lehrgänge, Fachseminare',
    'gewerkschaft':          'UFO, ver.di, Berufsverbände',
    'telefon_internet':      'Mobilfunk, DSL — anteilig 20% beruflich',
    'reinigung_uniform':     'Uniform-Reinigung über Pauschale (mit Beleg)',
    'reisekosten':           'Parken, Transport am Homebase (separat von BMF-Pauschalen)',
    'versicherung_beruf':    'Berufshaftpflicht, Diensthaftpflicht',
    'sonstige_beruf':        'Sonstige eindeutig berufliche Kosten',
    'nicht_erkannt':         'Klassifikator unsicher — landet im PDF-Anhang ohne Summe',
}
```

## §4 Inclusion-Regeln (Decision Tree)

```
A) confidence ≥ 0.85 + category ≠ nicht_erkannt + amount > 0
   → included_in_total = True
   → needs_review = False
   → source_type = user_document

B) confidence 0.50–0.84 + amount ≥ 14 € (money-relevant)
   → included_in_total = False
   → needs_review = True
   → review_question = „Ich habe diesen Beleg als mögliche {category} erkannt,
                       Betrag {amount} €. Soll er aufgenommen werden?"

C) confidence 0.50–0.84 + amount < 14 € (low-money)
   → included_in_total = False
   → needs_review = False
   → audit_note = „low-confidence + low-money, audit-only"

D) confidence < 0.50 ODER category = nicht_erkannt
   → included_in_total = False
   → needs_review = False
   → status = „nicht automatisch erkannt"
   → im PDF-Anhang aufgeführt mit Status, ohne Summe

E) User korrigiert via Chat
   → source_type = 'user' (Stern-Marker)
   → category = vom User bestätigt
   → included_in_total = True
   → audit_note = „user-confirmed YYYY-MM-DD"
```

## §5 Hard-Constraints (rechtlich + fachlich)

| Constraint | Erzwungen wo |
|---|---|
| KEIN automatischer Ansatz wenn category = 'sonstige_beruf' AND confidence < 0.95 | Klassifikator |
| KEINE Ansetzung wenn Beleg < 1 € (vermutlich Privat-Trash) | Klassifikator |
| KEINE Ansetzung wenn merchant in PRIVATE_BLACKLIST (Restaurant ohne Geschäftskontext, Lebensmittelladen, etc.) | Heuristik-Liste |
| Optional-Belege werden NICHT von Z77 verrechnet | Block A Berechnungs-Schicht |
| Optional-Belege werden NICHT von Z17 verrechnet | Block A Berechnungs-Schicht |
| Source-Label im PDF = „Hochgeladener Beleg" oder „Deine Angabe *" (wenn user-bestätigt) | PDF-Renderer |
| Klassifikator generiert NIE einen Steuerbetrag — nur Beleg-Beträge, gegen die Regeln gecheckt | Architektur-Prinzip |

## §6 Datenfluss

```
[Upload Frontend]
   ↓ files
[POST /api/upload/optional-receipts]
   ↓ stored
[Background: parse_optionale_belege] (Sonnet Vision)
   ↓ raw OCR + extracted fields
[classify_receipt(parsed)] — Python deterministic
   ↓ Inclusion-Regeln §4
[result.optionale_belege[]] — full schema §2
   ↓ join into result_dict
[Frontend: opt-receipt-summary] — counts only
[Frontend: chat review] — only if needs_review=True
[PDF: §§ 4 + 5] — recognized table + appendix
```

## §7 PDF-Darstellung

Nach „Einzutragender Gesamtbetrag":

### §4 Eingerechnete optionale Belege

| Datum | Kategorie | Beschreibung | Betrag | Quelle | Status |
|---|---|---|---|---|---|
| 15.01.2025 | Reinigung/Uniform | Uniform-Werkstatt GmbH | 89,90 € | Hochgeladener Beleg | aufgenommen |
| 08.03.2025 | Weiterbildung | LH-Sprachkurs Englisch | 240,00 € | Hochgeladener Beleg * | aufgenommen (von dir bestätigt) |

### §5 Nicht automatisch erkannte Belege

| Dateiname | Grund | Status |
|---|---|---|
| beleg_unknown_1234.pdf | Kategorie unklar | im Anhang |
| receipt_low_conf.jpg | Betrag nicht lesbar | im Anhang |

> Diese Belege wurden angehängt, aber nicht automatisch in die Summe übernommen.

## §8 Frontend-UI

Ein `<div id="opt-receipt-summary">` zeigt nach Upload:

```
Belege-Zusammenfassung
12 hochgeladen  •  8 erkannt  •  3 zur Prüfung  •  1 nicht erkannt
[Details anzeigen] [Später prüfen] [Nicht erkannte im Anhang aufführen]
```

Keine 50-Zeilen-Tabelle direkt sichtbar. „Details anzeigen" öffnet einen Drawer mit Liste.

## §9 Test-Plan

Neue Backend-Tests (pytest):

| Test | Was er prüft |
|---|---|
| `test_receipt_high_confidence_included` | Regel A |
| `test_receipt_money_relevant_ambiguous_creates_review` | Regel B |
| `test_receipt_low_money_low_confidence_no_review` | Regel C |
| `test_receipt_unrecognized_listed_in_appendix_not_in_total` | Regel D |
| `test_receipt_user_confirmed_marked_with_star` | Regel E |
| `test_optional_receipts_not_offset_by_z77` | Hard-Constraint |
| `test_optional_receipts_not_offset_by_z17` | Hard-Constraint |
| `test_classifier_no_tax_amount_without_rule` | Architektur-Prinzip |
| `test_private_blacklist_merchant_not_auto_included` | Heuristik |
| `test_pdf_renders_recognized_table` | PDF-Output |
| `test_pdf_renders_unrecognized_appendix` | PDF-Output |
| `test_receipt_50_file_limit_enforced` | Limit |

## §10 Was JETZT eingebaut wurde (Frontend Only)

- `<div id="opt-dropzone">` mit single file input (`#f-opt-any`, multiple, accept image/pdf/heic)
- `window.uploadOptAny(inp)` JS-Handler — appendet bis 50 Files in `ups['auto']`, ruft `_persistUploadToBackend('opt_auto', ...)`
- `<div id="opt-receipt-summary">` Liquid-Glass-Card, zeigt vorerst nur Count + Placeholder „Klassifikation startet bei Auswertung"
- Bestehende Kategorienwand bleibt unter „Belege nach Kategorie zuordnen (optional) ▾" als kollabiertes `<details>` für Power-User

## §11 Was OFFEN bleibt (Backend-Sprint)

- `classify_receipt(parsed)` Python-Funktion mit Regeln §4
- `/api/upload/optional-receipts` Endpoint mit 50-Limit-Enforcement
- `parse_optionale_belege` Erweiterung um `category_confidence`, `amount_confidence`, `merchant`
- Result-Dict-Felder `optionale_belege[]` voll-schema §2
- PDF-Renderer §§ 4 + 5
- Review-Generator: `needs_review` → Review-Item-Push in `_review_items`
- 12 Backend-Tests aus §9
- Hard-Constraint Tests (Z77/Z17 nicht offset)

## §12 Recommendation

**Frontend-Only Stub deployed jetzt** — User sieht die saubere UX, Upload funktioniert via existing persist-channel.
**Backend-Implementation als separater Sprint** — braucht eigenen gcloud deploy + 12 neue Tests.

Keine Vermischung mit Berechnungslogik (Z77/Z17/VMA) — optionale Belege bleiben strikt separater Topf, der erst nach Backend-Sprint Werte in die PDF-Tabelle §4 schreibt.
