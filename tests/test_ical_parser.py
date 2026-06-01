"""W19 iCal-Parser-Tests · synthetisches LH-Crew-Fixture.

Memory-Regel (siehe MEMORY.md feedback_ical_no_interpret.md):
"iCal 1:1 lesen — keine Office-Fallbacks erfinden, exakt was im LH-Kalender steht."

Coverage:
- F1: TZ-Bucket aus LOKAL-Datum (Europe/Berlin) statt UTC
- F2: Multi-Day-Tour expandiert auf alle Tage
- F3: All-Day-Event DTEND-exklusiv
- F4: RRULE COUNT inkl. Master-Event (off-by-one fix)
- F5: Same-Day-Merge (mehrere Events am selben Tag werden konsolidiert)
- F6: Tolerantes Parsen (DATE-only DTSTART, fehlendes DTEND)
"""
import os
import sys
import re

# Path-Setup damit `import app` aus dem Repo-Root funktioniert.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend


FIXTURE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'fixtures', 'lh_synthetic.ics')


def _load_fixture_events():
    with open(FIXTURE_PATH) as f:
        text = f.read()
    return backend._parse_ics_to_events(text)


def _load_fixture_briefings():
    evs = _load_fixture_events()
    briefings, _ = backend._ics_events_to_briefings(evs, existing={})
    return briefings


def test_parses_all_events_from_fixture():
    """Fixture hat 10 VEVENTs + 4 RRULE-Expansions (Urlaub COUNT=5 → 4 extra).
    Erwartung: ≥10 Events (Master) + 4 Expansions = 14.
    """
    events = _load_fixture_events()
    # Master-Events: 10 VEVENTs in der Fixture
    masters = [e for e in events if not e.get('_recurrence_of')]
    recurrences = [e for e in events if e.get('_recurrence_of')]
    assert len(masters) == 10, f'expected 10 master events, got {len(masters)}'
    # F4: COUNT=5 inkl. Master → 4 Expansions
    assert len(recurrences) == 4, (
        f'F4 broken: expected 4 RRULE expansions (COUNT=5 inkl. Master), '
        f'got {len(recurrences)}')


def test_f1_tz_bucket_uses_local_date_not_utc():
    """Briefing 23:30 Berlin lokal am Jun 12 → bucket muss Jun 12 sein.
    Vorher: 22:30 UTC am Jun 12 → bucket auch Jun 12 (Sommer-DST OK), aber
    bei Winter-Events 01:30 Berlin → 00:30 UTC Vortag (Bug).
    Hier verifizieren wir das Sommer-Sample und die Konsistenz lokal→bucket.
    """
    events = _load_fixture_events()
    late = [e for e in events if (e.get('summary') or '').startswith('Late Briefing')]
    assert late, 'Late Briefing event not parsed'
    e = late[0]
    # 23:30 Berlin am Jun 12 (Sommer, MESZ +02:00) → lokal Jun 12
    assert e['start'] == '2026-06-12', f"F1: expected Jun 12, got {e['start']}"
    # UTC-Iso ist 21:30Z am Jun 12 (MESZ +02:00)
    assert e['start_iso'] == '2026-06-12T21:30:00Z', (
        f"unexpected UTC iso: {e['start_iso']}")
    # End-Datum lokal: 01:30 Berlin am Jun 13 → bucket Jun 13
    assert e['end'] == '2026-06-13', f"F1-end: expected Jun 13, got {e['end']}"


def test_f2_multiday_tour_expands_to_all_days():
    """SIN-Tour Jun 25 14:00 → Jun 27 12:00 → drei Briefing-Tage Jun 25/26/27."""
    briefings = _load_fixture_briefings()
    for d in ('2026-06-25', '2026-06-26', '2026-06-27'):
        assert d in briefings, f'F2: day {d} missing in briefings'
        s = briefings[d].get('ical_summary') or ''
        assert 'SIN' in s.upper(), f'F2: day {d} should have SIN summary, got "{s}"'


def test_f3_all_day_dtend_exclusive():
    """Krank-Event DTSTART=Jun 15 / DTEND=Jun 16 → nur Jun 15, NICHT Jun 16."""
    briefings = _load_fixture_briefings()
    assert '2026-06-15' in briefings, 'F3: Jun 15 (Krank) missing'
    k = briefings['2026-06-15'].get('ical_summary') or ''
    assert 'Krank' in k, f'F3: Jun 15 should be Krank, got "{k}"'
    # Jun 16 hat einen anderen Event (OFFICE FRA) — der Krank-Marker darf da
    # NICHT mehr reinleaken (DTEND ist exklusiv).
    b16 = briefings.get('2026-06-16', {})
    s16 = (b16.get('ical_summary') or '')
    assert 'Krank' not in s16, (
        f'F3 broken: Krank leaked into Jun 16 via inclusive DTEND. '
        f'Got: "{s16}"')


def test_f4_rrule_count_includes_master():
    """Urlaub RRULE COUNT=5 startet Jun 18 → muss 5 Tage in Folge sein
    (Jun 18..22), NICHT 6 (Master + 5 Expansions wäre off-by-one).
    """
    briefings = _load_fixture_briefings()
    urlaub_days = sorted([d for d, b in briefings.items()
                          if 'Urlaub' in (b.get('ical_summary') or '')])
    assert len(urlaub_days) == 5, (
        f'F4 broken: expected 5 Urlaub days (COUNT=5 inkl. Master), '
        f'got {len(urlaub_days)}: {urlaub_days}')
    assert urlaub_days[0] == '2026-06-18'
    assert urlaub_days[-1] == '2026-06-22'


def test_f5_same_day_merge():
    """Jun 16: OFFICE FRA 09:00-17:00 + Pickup 14:30 14:30-15:00 →
    beide Summaries müssen erhalten bleiben (concat), nicht überschreiben.
    """
    briefings = _load_fixture_briefings()
    b16 = briefings.get('2026-06-16')
    assert b16, 'F5: Jun 16 missing'
    s = b16.get('ical_summary') or ''
    assert 'OFFICE' in s.upper(), f'F5: OFFICE FRA verloren in "{s}"'
    assert 'PICKUP' in s.upper() or '1430' in s, (
        f'F5: Pickup 1430 verloren in "{s}"')
    # earliest start_iso = OFFICE (07:00 UTC = 09:00 Berlin MESZ)
    assert b16.get('ical_start_iso', '').startswith('2026-06-16T07:'), (
        f'F5: earliest start should be 07:00Z (OFFICE), '
        f'got {b16.get("ical_start_iso")}')


def test_f6_office_day_with_location_only():
    """OFFICE FRA Jun 16 09–17: SUMMARY=OFFICE FRA, LOCATION=FRA, hat Zeit.
    Memory-Regel: kein Fallback erfinden. Wenn SUMMARY da ist, das nutzen.
    """
    briefings = _load_fixture_briefings()
    b = briefings.get('2026-06-16')
    assert b, 'F6: Jun 16 OFFICE missing'
    assert b.get('ical_location') and 'FRA' in b['ical_location'].upper()
    # 1:1: 'OFFICE FRA' im Summary
    assert 'OFFICE' in (b.get('ical_summary') or '').upper()


def test_long_haul_jfk_cross_tz_start():
    """LH416 FRA-JFK: DTSTART Berlin 08:00, DTEND New_York 12:00 (Multi-TZ).
    Bucket = lokal Berlin → Jun 10. Cross-Day-Boundary darf nicht passieren.
    """
    events = _load_fixture_events()
    jfk = [e for e in events if 'JFK' in (e.get('summary') or '')]
    assert jfk, 'LH416 not parsed'
    assert jfk[0]['start'] == '2026-06-10', (
        f"expected Jun 10 start bucket, got {jfk[0]['start']}")


def test_cancelled_events_skipped():
    """STATUS:CANCELLED → Event darf nicht im Output erscheinen.
    Wir prüfen das mit einem inline-Mini-ICS.
    """
    ics = (
        'BEGIN:VCALENDAR\r\n'
        'BEGIN:VEVENT\r\n'
        'UID:x@y\r\n'
        'SUMMARY:Cancelled Briefing\r\n'
        'DTSTART;TZID=Europe/Berlin:20260701T080000\r\n'
        'DTEND;TZID=Europe/Berlin:20260701T090000\r\n'
        'STATUS:CANCELLED\r\n'
        'END:VEVENT\r\n'
        'END:VCALENDAR\r\n'
    )
    evs = backend._parse_ics_to_events(ics)
    assert evs == [], f'cancelled event leaked: {evs}'


def test_line_folding_rfc5545():
    """RFC-5545 §3.1: SPACE/TAB-Continuation muss unfolded werden."""
    ics = (
        'BEGIN:VCALENDAR\r\n'
        'BEGIN:VEVENT\r\n'
        'SUMMARY:Long Description that wraps\r\n'
        '  across two lines via folding\r\n'
        'DTSTART;TZID=Europe/Berlin:20260801T080000\r\n'
        'END:VEVENT\r\n'
        'END:VCALENDAR\r\n'
    )
    evs = backend._parse_ics_to_events(ics)
    assert len(evs) == 1
    assert 'across two lines' in (evs[0].get('summary') or '')


if __name__ == '__main__':
    # Standalone-Lauf: gibt 8+-Tag-Output aus, damit man sieht was der Parser
    # macht. Erlaubt schnellen Sanity-Check.
    evs = _load_fixture_events()
    briefings = _load_fixture_briefings()
    print(f'\n=== _parse_ics_to_events → {len(evs)} events ===')
    for e in evs:
        print(f"  {e.get('start','???')} → {e.get('end','???')} "
              f"[{e.get('summary','')[:40]}] @{e.get('location','')[:10]} "
              f"iso={e.get('start_iso','-')} multiday={len(e.get('_multiday_dates') or [])}")
    print(f'\n=== _ics_events_to_briefings → {len(briefings)} days ===')
    for d in sorted(briefings.keys()):
        b = briefings[d]
        print(f"  {d}: summary='{b.get('ical_summary','')[:50]}' "
              f"loc='{b.get('ical_location','')[:20]}' "
              f"start={b.get('ical_start_iso','-')} end={b.get('ical_end_iso','-')}")
    print('\nRunning assertion tests…')
    test_parses_all_events_from_fixture()
    test_f1_tz_bucket_uses_local_date_not_utc()
    test_f2_multiday_tour_expands_to_all_days()
    test_f3_all_day_dtend_exclusive()
    test_f4_rrule_count_includes_master()
    test_f5_same_day_merge()
    test_f6_office_day_with_location_only()
    test_long_haul_jfk_cross_tz_start()
    test_cancelled_events_skipped()
    test_line_folding_rfc5545()
    print('ALL TESTS PASSED')
