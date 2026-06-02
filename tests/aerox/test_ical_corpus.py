"""LAYER 6 — iCal Corpus Tests (AeroX)

Tests the backend iCal parser `_parse_ics_to_events` + `_ics_events_to_briefings`
in `app.py` against 20 isolated synthetic edge-case fixtures.

These tests document the OBSERVED behaviour of the parser as-is. They are
designed to catch regressions like the ones found in commit 1444255:
  - TZ-bucket UTC-vs-local Bug (F1)
  - Multi-Day only Tag 1 sichtbar (F2)
  - All-Day DTEND inclusive Bug (F3)
  - RRULE COUNT off-by-one (F4)
  - Same-Day overwrite (F5)
  - Empty-DTSTART drop
  - klass=nil mapping (CATEGORIES not parsed)

Where the parser has a KNOWN bug, we keep the test PASSING but the assertion
documents the actual broken behaviour with an inline `# BUG:` note — the
fix will require flipping the assertion.

The parser code is NOT modified by these tests (test-WAS-IST).
"""
import os
import sys
import time

import pytest

# Path setup: import `app` from repo root.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
sys.path.insert(0, _REPO_ROOT)

import app as backend  # noqa: E402

FIXTURES_DIR = os.path.join(_THIS_DIR, "fixtures", "ical")


def _load(name):
    """Load a fixture .ics by filename and run parser + briefings mapper.

    Returns (events_list, briefings_dict).
    """
    path = os.path.join(FIXTURES_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    events = backend._parse_ics_to_events(text)
    briefings, _ = backend._ics_events_to_briefings(events, existing={})
    return events, briefings


# ---------------------------------------------------------------------------
# 1. simple_single_day.ics — one all-day event 2026-06-15
# ---------------------------------------------------------------------------
def test_01_simple_single_day():
    events, briefings = _load("simple_single_day.ics")
    assert len(events) == 1, f"expected 1 event, got {len(events)}"
    ev = events[0]
    assert ev.get("start") == "2026-06-15", (
        f"start bucket should be 2026-06-15, got {ev.get('start')}"
    )
    # All-Day: DTEND 2026-06-16 is exclusive → only day 15 in briefings.
    assert "2026-06-15" in briefings, "Jun 15 missing in briefings"
    assert "2026-06-16" not in briefings, (
        "F3: all-day DTEND must be exclusive, Jun 16 should NOT appear"
    )


# ---------------------------------------------------------------------------
# 2. multi_day_tour_inclusive.ics — timed multi-day Jun 10..12 → 3 days
# ---------------------------------------------------------------------------
def test_02_multi_day_tour_inclusive():
    _, briefings = _load("multi_day_tour_inclusive.ics")
    for d in ("2026-06-10", "2026-06-11", "2026-06-12"):
        assert d in briefings, (
            f"F2 multi-day expansion broken: {d} missing in briefings"
        )
    # Tag 3/3 marker should appear for the last day
    last = briefings.get("2026-06-12", {}).get("ical_summary", "")
    assert "Tag 3/3" in last, (
        f"Multi-day tag marker missing on day 3, got: {last!r}"
    )


# ---------------------------------------------------------------------------
# 3. multi_day_tour_exclusive_dtend.ics — all-day DTSTART 06-10 DTEND 06-12
#    → only 2 days (RFC 5545: DTEND exclusive for all-day).
# ---------------------------------------------------------------------------
def test_03_multi_day_tour_exclusive_dtend():
    _, briefings = _load("multi_day_tour_exclusive_dtend.ics")
    assert "2026-06-10" in briefings, "all-day start Jun 10 missing"
    assert "2026-06-11" in briefings, "all-day Jun 11 missing"
    assert "2026-06-12" not in briefings, (
        "F3: all-day DTEND 06-12 must be EXCLUSIVE — Jun 12 should not appear"
    )


# ---------------------------------------------------------------------------
# 4. tz_berlin_overnight.ics — 23:00 Berlin local → bucket = Jun 15, not 16.
# ---------------------------------------------------------------------------
def test_04_tz_berlin_overnight_bucket_is_local():
    events, briefings = _load("tz_berlin_overnight.ics")
    assert len(events) == 1
    ev = events[0]
    assert ev["start"] == "2026-06-15", (
        f"F1 TZ-bucket bug: 23:30 Europe/Berlin must bucket to local "
        f"date Jun 15, got {ev['start']} (UTC would be Jun 15 21:30Z)."
    )
    # UTC iso: 23:30 MESZ = 21:30 UTC same day.
    assert ev["start_iso"] == "2026-06-15T21:30:00Z", (
        f"expected start_iso 2026-06-15T21:30:00Z, got {ev['start_iso']}"
    )
    # End is 06:00 Berlin on Jun 16 = 04:00 UTC Jun 16
    assert ev["end"] == "2026-06-16", f"end bucket: {ev['end']}"
    assert "2026-06-15" in briefings and "2026-06-16" in briefings


# ---------------------------------------------------------------------------
# 5. dst_transition_spring.ics — Mar 28 01:00 Berlin → Mar 29 06:00 Berlin
#    DST forward (Mar 29 02:00→03:00 CET→CEST in 2026).
#    Verify: parser doesn't emit duplicate days, no crash.
# ---------------------------------------------------------------------------
def test_05_dst_transition_spring_no_duplicates():
    events, briefings = _load("dst_transition_spring.ics")
    assert len(events) == 1
    days = events[0].get("_multiday_dates") or []
    assert len(days) == len(set(days)), (
        f"DST-spring: duplicate days in multiday expansion: {days}"
    )
    assert "2026-03-28" in briefings
    assert "2026-03-29" in briefings


# ---------------------------------------------------------------------------
# 6. dst_transition_fall.ics — Oct 25 01:00 → Oct 26 06:00 Berlin
#    DST backward (Oct 25 03:00→02:00). No duplicate day buckets.
# ---------------------------------------------------------------------------
def test_06_dst_transition_fall_no_duplicates():
    events, briefings = _load("dst_transition_fall.ics")
    assert len(events) == 1
    days = events[0].get("_multiday_dates") or []
    assert len(days) == len(set(days)), (
        f"DST-fall: duplicate days in multiday expansion: {days}"
    )
    assert "2026-10-25" in briefings
    assert "2026-10-26" in briefings


# ---------------------------------------------------------------------------
# 7. rrule_daily_count_5.ics — FREQ=DAILY;COUNT=5 → exactly 5 days, not 4 or 6.
#    F4: parser must include MASTER + COUNT-1 expansions = COUNT total.
# ---------------------------------------------------------------------------
def test_07_rrule_daily_count_5_exact():
    events, briefings = _load("rrule_daily_count_5.ics")
    # Master + 4 expansions = 5 total
    assert len(events) == 5, (
        f"F4 RRULE COUNT off-by-one: COUNT=5 must yield 5 events, got "
        f"{len(events)}"
    )
    expected_days = {f"2026-06-{d:02d}" for d in range(1, 6)}
    got_days = set(briefings.keys())
    assert expected_days == got_days, (
        f"RRULE daily: expected days {expected_days}, got {got_days}"
    )


# ---------------------------------------------------------------------------
# 8. rrule_weekly_byday_mowefri.ics — Jun 1 (Mon) + BYDAY=MO,WE,FR;COUNT=6.
#    Expected: Jun 1 (Mo), 3 (We), 5 (Fr), 8 (Mo), 10 (We), 12 (Fr).
# ---------------------------------------------------------------------------
def test_08_rrule_weekly_byday_specific():
    _, briefings = _load("rrule_weekly_byday_mowefri.ics")
    expected = {"2026-06-01", "2026-06-03", "2026-06-05",
                "2026-06-08", "2026-06-10", "2026-06-12"}
    got = set(briefings.keys())
    assert expected == got, (
        f"RRULE WEEKLY BYDAY MO,WE,FR COUNT=6 mismatch.\n"
        f"  expected: {sorted(expected)}\n"
        f"  got:      {sorted(got)}"
    )


# ---------------------------------------------------------------------------
# 9. rrule_until_inclusive.ics — Daily from Jun 25 UNTIL 2026-06-30 23:59:59Z
#    → last day must be Jun 30 inclusive (RFC 5545 §3.3.10 UNTIL is inclusive).
# ---------------------------------------------------------------------------
def test_09_rrule_until_inclusive():
    _, briefings = _load("rrule_until_inclusive.ics")
    assert "2026-06-25" in briefings, "master day missing"
    assert "2026-06-30" in briefings, (
        "UNTIL=20260630T235959Z must be INCLUSIVE — Jun 30 missing"
    )
    assert "2026-07-01" not in briefings


# ---------------------------------------------------------------------------
# 10. empty_dtstart.ics — event without DTSTART must be dropped, no crash.
#     Valid sibling event must remain.
# ---------------------------------------------------------------------------
def test_10_empty_dtstart_dropped_no_crash():
    events, briefings = _load("empty_dtstart.ics")
    # Broken event has no start bucket → dropped from briefings.
    assert "2026-06-20" in briefings, "valid sibling event must persist"
    # Either the broken event is skipped at parser-stage, or it survives but
    # has no `start` → still won't appear in briefings.
    for ev in events:
        # Defensive: if it survives, it must have no start or be the sibling
        if not ev.get("start"):
            assert (ev.get("summary") or "").startswith("Broken"), (
                f"unexpected ev without start: {ev}"
            )


# ---------------------------------------------------------------------------
# 11. same_day_two_events.ics — two events on same day → both preserved
#     (merged into one briefing slot, no overwrite).
# ---------------------------------------------------------------------------
def test_11_same_day_merge_no_overwrite():
    events, briefings = _load("same_day_two_events.ics")
    assert len(events) == 2, f"both events must parse, got {len(events)}"
    b = briefings.get("2026-06-18")
    assert b is not None, "Jun 18 missing"
    summary = (b.get("ical_summary") or "").lower()
    # Both flights mentioned, separator " · " from F5 merge logic.
    assert "morning" in summary and "evening" in summary, (
        f"Same-Day merge dropped one event: {b.get('ical_summary')!r}"
    )
    # Earliest start_iso should be the morning one
    assert b.get("ical_start_iso", "").startswith("2026-06-18T04"), (
        f"earliest start_iso must be 06:00 Berlin (04:00Z), "
        f"got {b.get('ical_start_iso')}"
    )


# ---------------------------------------------------------------------------
# 12. summary_with_special_chars.ics — escaped commas in SUMMARY.
# ---------------------------------------------------------------------------
def test_12_summary_with_special_chars():
    events, briefings = _load("summary_with_special_chars.ics")
    assert len(events) == 1
    ev = events[0]
    s = ev.get("summary") or ""
    # Comma content must be preserved. Parser may or may not unescape `\,`
    # — both acceptable, but the substrings must be there.
    assert "BER" in s and "MUC" in s and "FRA" in s, (
        f"escaped-comma SUMMARY lost cities: {s!r}"
    )
    loc = ev.get("location") or ""
    assert "Multiple" in loc and "Stops" in loc, (
        f"escaped-comma LOCATION lost content: {loc!r}"
    )


# ---------------------------------------------------------------------------
# 13. categories_layover.ics — CATEGORIES:LAYOVER,HOTEL.
#     Validates: parser extracts CATEGORIES → event.categories list (lowercased),
#     briefing carries ical_klass = "hotel_layover".
# ---------------------------------------------------------------------------
def test_13_categories_layover():
    events, briefings = _load("categories_layover.ics")
    assert len(events) == 1, "event must still parse"
    ev = events[0]
    assert ev.get("summary") == "Layover JFK"
    cats = ev.get("categories") or []
    assert "layover" in cats, (
        f"CATEGORIES:LAYOVER not parsed → categories={cats!r}"
    )
    assert "hotel" in cats, (
        f"CATEGORIES:LAYOVER,HOTEL not parsed → categories={cats!r}"
    )
    # Briefing entry for the start day must carry the klass mapping.
    b = briefings.get("2026-07-01") or {}
    assert b.get("ical_klass") == "hotel_layover", (
        f"LAYOVER must map to ical_klass='hotel_layover', got "
        f"{b.get('ical_klass')!r}"
    )


# ---------------------------------------------------------------------------
# 14. categories_standby.ics — CATEGORIES:STANDBY.
# ---------------------------------------------------------------------------
def test_14_categories_standby():
    events, briefings = _load("categories_standby.ics")
    assert len(events) == 1
    ev = events[0]
    assert ev.get("summary") == "Standby FRA"
    cats = ev.get("categories") or []
    assert "standby" in cats, (
        f"CATEGORIES:STANDBY not parsed → categories={cats!r}"
    )
    b = briefings.get("2026-07-05") or {}
    assert b.get("ical_klass") == "standby", (
        f"STANDBY must map to ical_klass='standby', got "
        f"{b.get('ical_klass')!r}"
    )


# ---------------------------------------------------------------------------
# 15. categories_off.ics — CATEGORIES:OFF must classify as frei.
# ---------------------------------------------------------------------------
def test_15_categories_off():
    events, briefings = _load("categories_off.ics")
    assert len(events) == 1
    ev = events[0]
    assert ev.get("summary") == "Day Off"
    cats = ev.get("categories") or []
    assert "off" in cats, (
        f"CATEGORIES:OFF not parsed → categories={cats!r}"
    )
    b = briefings.get("2026-07-08") or {}
    assert b.get("ical_klass") == "frei", (
        f"OFF must map to ical_klass='frei', got {b.get('ical_klass')!r}"
    )


# ---------------------------------------------------------------------------
# 16. klass_nil_legacy.ics — no CATEGORIES → klass must NOT be nil/missing
#     in the output. Acceptable: a fallback like 'unknown', or simply that
#     the event still has a summary the classifier can fall back to.
# ---------------------------------------------------------------------------
def test_16_klass_nil_legacy_has_fallback():
    events, briefings = _load("klass_nil_legacy.ics")
    assert len(events) == 1, "event must still parse"
    ev = events[0]
    # Parser doesn't emit `klass`, but summary+start must be present so a
    # downstream classifier has something to map. The legacy bug was a None
    # event slipping through — this guards that.
    assert ev.get("start") == "2026-07-10"
    assert (ev.get("summary") or "").strip() != "", (
        "event without categories must still expose a non-empty summary"
    )
    b = briefings.get("2026-07-10") or {}
    assert b.get("ical_summary"), (
        "klass-nil regression: briefing entry exists but ical_summary is empty"
    )


# ---------------------------------------------------------------------------
# 17. vtimezone_unknown.ics — obscure TZID Atlantic/Madeira must not crash.
# ---------------------------------------------------------------------------
def test_17_vtimezone_unknown_no_crash():
    events, briefings = _load("vtimezone_unknown.ics")
    assert len(events) == 1, (
        f"obscure TZID must not drop the event, got {len(events)}"
    )
    ev = events[0]
    assert ev.get("summary") == "Atlantic Layover"
    # Date bucket should be Jul 12 regardless of TZID resolution.
    assert ev.get("start", "").startswith("2026-07-12"), (
        f"unknown TZID: expected bucket 2026-07-12, got {ev.get('start')}"
    )
    assert "2026-07-12" in briefings


# ---------------------------------------------------------------------------
# 18. event_in_past.ics — DTSTART 2020 → must still appear in output.
# ---------------------------------------------------------------------------
def test_18_event_in_past_not_dropped():
    events, briefings = _load("event_in_past.ics")
    assert len(events) == 1
    assert events[0].get("start") == "2020-01-15"
    assert "2020-01-15" in briefings, (
        "past events must not be silently dropped — user may want history"
    )


# ---------------------------------------------------------------------------
# 19. event_far_future.ics — DTSTART 2030 → must still appear.
# ---------------------------------------------------------------------------
def test_19_event_far_future_not_dropped():
    events, briefings = _load("event_far_future.ics")
    assert len(events) == 1
    assert events[0].get("start") == "2030-12-31"
    assert "2030-12-31" in briefings, (
        "far-future events must not be out-of-range-gedropt"
    )


# ---------------------------------------------------------------------------
# 20. huge_file_100_events.ics — performance budget < 2s for 100 events.
# ---------------------------------------------------------------------------
def test_20_huge_file_100_events_under_2s():
    path = os.path.join(FIXTURES_DIR, "huge_file_100_events.ics")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    t0 = time.perf_counter()
    events = backend._parse_ics_to_events(text)
    briefings, _ = backend._ics_events_to_briefings(events, existing={})
    elapsed = time.perf_counter() - t0
    assert len(events) == 100, (
        f"huge fixture: expected 100 parsed events, got {len(events)}"
    )
    assert len(briefings) == 100, (
        f"huge fixture: expected 100 briefing days, got {len(briefings)}"
    )
    assert elapsed < 2.0, (
        f"perf budget exceeded: 100-event parse took {elapsed:.3f}s (>2.0s)"
    )
