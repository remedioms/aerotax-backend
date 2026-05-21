# AeroTAX — Relevant Code Snippets for Review

All snippets from `app.py` (single-file Flask backend, ~21000 lines total).

> **Note:** This file lists code locations + brief summaries. For full source, see snippets below or reference original line ranges.

---

## 1. `_deterministic_classify_v7` — main classifier

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 14466 – 15700 (approx 1230 lines) |
| Purpose | Per-day classification: Z72/Z73/Z74/Z76/Office/Standby/ZeroDay/Issue/Frei |

**Signature:**
```python
def _deterministic_classify_v7(matched_days, year=2025, homebase='FRA', commute_minutes=0):
    """Day-by-day classifier. Takes matched-days = list of {datum, dp, se}-dicts
    where dp = parsed CAS day-fact, se = parsed SE-day-fact.
    Returns dict with tage_detail, _klass_summary, _review_items, audit-lists."""
```

**Key per-day decision tree (high-level):**
```
for each day d:
  if activity_type in ('frei', 'urlaub', 'krank'):
    klass = 'Frei'
  elif activity_type == 'office' or 'training':
    if marker passive (ORTSTAG/FRS/LMN_AS/LMN_CR) and no duty:
      klass = 'Office' (passive)
    elif Inland duty >= 480 min:
      klass = 'Z72' (14 €)
    else:
      klass = 'Office'
  elif activity_type == 'same_day':
    if has_fl or overnight (today):
      klass = 'Issue' (Hard-Gate-Violation)
    elif prev_overnight:
      # ── BH-003a 2026-05-19: chirurgischer rescue ──
      if (prev.layover not inland, ends_at_homebase, routing[0]==prev.layover,
          routing[-1]==homebase, duty>=480, BMF-mapping):
        klass = 'Z76'   # Tour-Heimkehr An/Ab
      else:
        klass = 'Issue' ("Heimkehr aus Vortag-Tour")
    elif in foreign cluster:
      klass = 'Z76' (28 €)
    elif active foreign SE:
      klass = 'Z76' (BMF an_abreise satz)
    elif inland-routing-roundtrip + duty>=480:
      klass = 'Z72'
    else:
      klass = 'ZeroDay'
  elif activity_type == 'tour':
    if overnight + layover_inland:
      klass = 'Z73' (14 €)
    elif overnight + layover_foreign:
      klass = 'Z76' (BMF voll_24h or an_abreise)
    elif evening-foreign-tour-start (briefing>=18:00):
      klass = 'Z73' (14 €, inland-anreise-rule)
    ...
  elif activity_type == 'standby':
    klass = 'Standby'
  ...
```

### BH-003a snippet (Z.15070-15125)

```python
elif prev_overnight:
    # v8.15: Same-Day mit prev_overnight + aktive Auslands-SE → Z76
    if (se.get('count', 0) > 0 and se.get('stfrei_inland') is False
            and se.get('stfrei_ort')):
        se_ort_v15 = se.get('stfrei_ort', '')
        bmf_aus_v15 = _bmf(se_ort_v15)
        eur_added = float((bmf_aus_v15.get('an_abreise', 0) if bmf_aus_v15 else 28.0) or 0)
        klass = 'Z76'
        reason = f'Same-Day Auslandstrip {se_ort_v15} (Z76 >8h, prev_overnight=true Sonnet-Lesefehler)'
        ...
    else:
        # ── BH-003a 2026-05-19: Chirurgischer Heimkehr-Rescue ──
        # User-Beweis Tibor 2025-01-06: Issue mit reason „Heimkehr aus
        # Vortag-Tour" obwohl tatsächlich Z76-An/Ab-Tag (BLR→FRA, duty
        # 561min, ends_at_homebase). Golden klassifiziert das als
        # Z76 Indien-Bangalore An/Ab 28€.
        # Guards (alle müssen erfüllt sein, sonst Issue-Fallback):
        #   G1 prev.layover_ort nicht leer
        #   G2 prev.layover_ort kein Inland-Code (echter Auslands-Layover)
        #   G3 today.ends_at_homebase=True
        #   G4 today.routing[0] == prev.layover_ort (Direkt-Rückflug)
        #   G5 today.routing[-1] == homebase
        #   G6 today.duty_duration_minutes >= 480 (8h)
        #   G7 BMF-Mapping liefert ein Land
        # Schützt vor false-positives 05-23/06-03/10-28 (Frei laut Golden).
        _bh003a_layover = ((prev['dp'].get('layover_ort') if prev else '') or '').upper().strip()
        _bh003a_hb_up = (homebase or 'FRA').upper()
        _bh003a_routing = [(r or '').upper().strip() for r in (d.get('routing') or [])]
        _bh003a_duty = int(d.get('duty_duration_minutes') or 0)
        _bh003a_ends_hb = bool(d.get('ends_at_homebase'))
        if (_bh003a_layover                                            # G1
            and not _is_inland_code(_bh003a_layover)                   # G2
            and _bh003a_ends_hb                                        # G3
            and len(_bh003a_routing) >= 2
            and _bh003a_routing[0] == _bh003a_layover                  # G4
            and _bh003a_routing[-1] == _bh003a_hb_up                   # G5
            and _bh003a_duty >= 480                                    # G6
        ):
            _bh003a_bmf = _bmf(_bh003a_layover)
            if _bh003a_bmf and _bh003a_bmf.get('an_abreise', 0) > 0:   # G7
                klass = 'Z76'
                eur_added = float((_bh003a_bmf.get('an_abreise', 0) or 0))
                reason = (
                    f'BH-003a Tour-Heimkehr {_bh003a_layover}→{_bh003a_hb_up} '
                    f'(Z76 An/Ab, duty {_bh003a_duty}min ≥ 480)'
                )
        if klass != 'Z76':
            klass = 'Issue'
            reason = 'Heimkehr aus Vortag-Tour — separater Tour-Abschluss'
            unresolved_reason = 'same_day nach prev_overnight (Mischfall)'
```

---

## 2. `_followme_align_counters` — final KPI aggregator

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 13697 – 13800 |
| Purpose | Replaces day-by-day counters with **tour-based** aggregation matching FollowMe.aero convention |

```python
def _followme_align_counters(classification, matched_days, year=2025, homebase='FRA'):
    """v11 F3/F4: Post-Klassifikator FollowMe-Align.
    Korrigiert:
    - fahr_tage = N(Touren) + N(Solo-Office/Schulung mit Anfahrt)
    - arbeitstage = Σ Tour-Tage + Σ Solo-Office/Schulung
    - reinigungstage = Σ Tour-Tage mit Uniform
    - hotel_naechte = Σ Tour-Tage mit Z76 minus letzter Z76-Tag pro Tour
    """
    tours = _followme_identify_tours(tage_detail)
    fahr_tage_followme = len(tours)
    arbeitstage_followme = sum(
        1 for tour in tours for td in tour['days']
        if _followme_is_active_workday(td)
    )
    reinigungstage_followme = arbeitstage_followme
    hotel_naechte_followme = 0
    for tour in tours:
        z76_idxs = [idx for idx, td in enumerate(tour['days'])
                     if (td.get('klass') or '').lower() == 'z76']
        if z76_idxs:
            hotel_naechte_followme += len(z76_idxs) - 1
    classification['fahr_tage'] = fahr_tage_followme
    classification['arbeitstage'] = arbeitstage_followme
    classification['reinigungstage'] = reinigungstage_followme
    classification['hotel_naechte'] = hotel_naechte_followme
    return classification
```

---

## 3. `_followme_identify_tours` — tour-sequence-builder

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 13653 – 13694 |
| Purpose | Max-contiguous service-day sequence becomes a tour |

```python
def _followme_identify_tours(tage_detail, homebase='FRA'):
    """Eine Tour = maximale zusammenhängende Sequenz von Diensttagen,
    getrennt durch mind. einen Nicht-Diensttag (Frei/Urlaub/Krank/
    ZeroDay/Issue/Standby-zuhause)."""
    sorted_td = sorted([t for t in tage_detail if isinstance(t, dict) and t.get('datum')],
                       key=lambda t: t['datum'])
    tours = []
    i = 0
    while i < len(sorted_td):
        if not _followme_is_service_day(sorted_td[i], homebase):
            i += 1
            continue
        tour_days = [sorted_td[i]]
        j = i + 1
        while j < len(sorted_td) and _followme_is_service_day(sorted_td[j], homebase):
            tour_days.append(sorted_td[j])
            j += 1
        tours.append({'days': tour_days, 'tour_size': len(tour_days)})
        i = j
    return tours
```

---

## 4. `_followme_is_service_day` + `_followme_is_active_workday` + `_followme_is_passive_ortstag`

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 13610 – 13650 |
| Purpose | Definitions of „Tour-Continuation-Day" vs „Active-Workday" vs „Passive-Ortstag" |

```python
def _followme_is_service_day(tage_detail_entry, homebase='FRA'):
    """True wenn Tag in eine Tour gehört (Sequence-Building)."""
    klass = (tage_detail_entry.get('klass') or '').lower()
    if klass in ('frei', 'urlaub', 'krank', 'zeroday', 'issue'):
        return False
    if klass == 'standby':
        cr = tage_detail_entry.get('classifier_result') or {}
        return bool(cr.get('eur', 0) > 0 or tage_detail_entry.get('eur', 0) > 0)
    return True


def _followme_is_passive_ortstag(tage_detail_entry):
    """ORTSTAG-Tag: Office-Marker ohne Briefingzeit/duration."""
    klass = (tage_detail_entry.get('klass') or '').lower()
    if klass != 'office': return False
    rf = tage_detail_entry.get('reader_facts') or {}
    start_time = (rf.get('start_time') or '').strip()
    duration_min = int(rf.get('duration_minutes', 0) or 0)
    marker = (tage_detail_entry.get('marker') or '').upper()
    is_passive_marker = any(m in marker for m in ('ORTSTAG', 'FRS', 'FRD'))
    return is_passive_marker and not start_time and duration_min == 0


def _followme_is_active_workday(tage_detail_entry):
    """True wenn Tag in FollowMe als AT zählt."""
    if not _followme_is_service_day(tage_detail_entry):
        return False
    if _followme_is_passive_ortstag(tage_detail_entry):
        return False
    return True
```

---

## 5. `_get_bmf_for_iata` — BMF Auslandspauschalen lookup

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 12789 – 12880 |
| Purpose | Resolves IATA airport-code → BMF country-name → {voll_24h, an_abreise}-Sätze |

```python
def _get_bmf_for_iata(iata, year, _diag=None, _allow_ai_resolver=True, ...):
    """BMF-Auslandspauschale für IATA-Code.
    Phase 3 Source-Kaskade:
      1. IATA_TO_BMF (direkter Airport-Code)
      2. IATA_METRO_TO_BMF (Metro Area: CHI/ROM/STO/...)
      3. KI-Resolver kind='place_code' mit Airline-Crew-Kontext"""
    # 1. Direct IATA_TO_BMF
    land = IATA_TO_BMF.get(iata_upper)
    if land:
        return _land_to_satz(land)  # → {voll_24h, an_abreise}
    # 2. Metro-Alias
    land_metro = IATA_METRO_TO_BMF.get(iata_upper)
    if land_metro:
        satz = _land_to_satz(land_metro)
        satz['_source'] = 'metro_alias'
        return satz
    # 3. AI-Resolver (≥0.90 conf → auto, 0.70-0.90 → review-suggestion)
    ...
```

`bmf_data.py` (separate file) contains:
- `IATA_TO_BMF`: dict mapping ~600 airport codes to country/city names
- `IATA_METRO_TO_BMF`: ~12 multi-airport metro aliases
- `BMF_AUSLAND_BY_YEAR[2025]`: dict country → (voll_24h_eur, an_abreise_eur)

---

## 6. `_match_dp_se_per_day` — DP + SE matching

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 13980 – 14400 (approx) |
| Purpose | Joins per-day CAS facts (`dp`) with per-day SE-line facts (`se`) by date, builds enriched per-day record |

```python
def _match_dp_se_per_day(structured_days, se_structured, homebase='FRA'):
    """Liefert list of {datum, dp, se} dicts mit:
    - dp: enriched CAS facts (overnight_after_day, layover_ort, requires_commute, ...)
    - se: enriched SE facts (stfrei_total, stfrei_ort, stfrei_inland, ...)
    """
```

---

## 7. SE-Override block (Phase-1)

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | (inside _deterministic_classify_v7, multiple places) |
| Purpose | When reader-misread sets `Frei` but SE-line shows active foreign reimbursement → upgrade to Z76 |

```python
# Cluster A / Phase-1 SE-Override:
if klass == 'Frei' and se.get('count', 0) > 0 \
        and se.get('stfrei_inland') is False \
        and se.get('stfrei_ort'):
    # Frei trotz aktiver Auslands-SE → rescue zu Z76
    klass = 'Z76'
    eur_added = bmf.get('an_abreise', 28.0)
    rescues.append({
        'datum': datum,
        'rescue_type': 'frei_to_z76_active_foreign_se',
        ...
    })
```

This rescue is suspected of **overcorrecting** — turning legitimately-Frei days into Z76 when SE-stamp leaks across days. See `REVIEW_OPEN_QUESTIONS.md` Q3.

---

## 8. `_resolve_uncertain_fact_with_ai` — AI Marker-Semantik-Resolver

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 7749 – 7900+ |
| Purpose | Active AI resolver (Anthropic Sonnet 4.5) for `place_code`, `marker_semantics`, `cas_time_extraction`, `layover_place`, `tour_context` |

```python
def _resolve_uncertain_fact_with_ai(kind, context, job_id=None, datum=None,
                                     uncertain_fact='', _anthropic_client=None):
    """Returns:
      {
        'resolved': bool,
        'value': dict,          # KEINE Beträge/Steuersätze (Anti-Tax-Sanitizer)
        'confidence': float,    # 0.0-1.0
        'reason': str,
        'evidence': list[str],
        'needs_review': bool,
      }
    Garantien:
      - Verbotene value-Keys (amount/eur/rate/...) → reject
      - Invalid JSON → review-fallback
      - Cache pro (job_id, datum, kind, context-hash) für TTL Stunden
    """
```

Used in `_build_review_items` (BH-001 Fix) and `_get_bmf_for_iata` (Phase-3 metro fallback).

---

## 9. `_build_review_items` — User-question generator

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 17854 – 18000+ |
| Purpose | Converts classifier diagnostic-lists into user-facing review questions |

**BH-001 patch (2026-05-19):** office_training_time_missing-Branch ruft jetzt KI-Marker-Semantik-Resolver **vor** Frage-Bildung. ≥0.90 + passive-Semantik → silent-skip. Sonst Marker-Frage statt 8h-Symptom.

```python
_DETERMINISTIC_PASSIVE_MARKERS = ('ORTSTAG', 'FRS', 'LMN_AS', 'LMN_CR', 'FRD')
for c in (cls.get('office_training_time_missing_candidates', []) or []):
    if any(pm in marker.upper() for pm in _DETERMINISTIC_PASSIVE_MARKERS):
        continue  # silent skip
    ai_result = _resolve_uncertain_fact_with_ai(
        kind='marker_semantics',
        context={'marker': marker, 'activity_type': c.get('activity_type'),
                 'context': 'Airline-Crew-Dienstplan office/training marker. '
                            'Cockpit/Kabine Lufthansa-ähnliches Roster.'},
        ...
    )
    if ai_confidence >= 0.90 and 'passive' in ai_semantics:
        continue  # silent skip
    # else: build item with marker-semantics question (not 8h-symptom)
```

---

## 10. `_classify_job_state` — State-Machine (API Contract)

| Attribute | Value |
|---|---|
| File | `app.py` |
| Line range | 3062 – 3300 |
| Purpose | Maps raw job-status → canonical_state + user_title + user_message + next_actions for `/api/job/<id>` + `/api/session/<token>` |

Output keys:
- `canonical_state`: `done | needs_review | failed_retryable | failed_support | expired | deleted | processing | queued`
- `reason_code`: `WORKER_RESTARTED | UPLOAD_PERSIST_FAILED | ALIGN_FAILED | ACCESS_CODE_EXPIRED | ...`
- `user_title`, `user_message` (localized)
- `pdf_allowed: bool`
- `next_actions: [{type, label}]`

---

## 11. Frontend State-Machine helpers (index.html)

| Attribute | Value |
|---|---|
| File | `index.html` (Cloudflare Pages, separate file) |
| Functions | `_normalizeBackendState`, `_hardHideResultSections`, `_failedStateLocked`, `deriveUiState`, `canShowPdfDownload`, `_applyPdfVisibility` |

```javascript
// _normalizeBackendState: leitet canonical_state aus result_data ab wenn Backend null lieferte
window._normalizeBackendState = function(j) {
  var rd = j.result_data || {};
  var pendingReview = (rd._review_items || []).filter(it => it.status === 'pending');
  if (!j.canonical_state) {
    if (pendingReview.length > 0) j.canonical_state = 'needs_review';
    else if (rd.netto > 0 || rd.brutto > 0) j.canonical_state = 'done';
    else j.canonical_state = 'processing';
  }
  return j;
};

// _hardHideResultSections: enforces state-machine — failed never coexists with done UI
window._hardHideResultSections = function(opts) {
  // hides: result-amount-label, result-netto-display, hero-actions,
  //        chat-inline-host, all <details>, dl-btn-row, header-pdf-btn, ...
};
```
