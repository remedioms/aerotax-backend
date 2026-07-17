import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
import app as A

def _day(datum, klass=None, routing=None, start=None, end=None, lay=None):
    return {'datum': datum, 'klass': klass, 'routing': routing,
            'reader_facts': {'start_time': start, 'end_time': end, 'layover_ort': lay}}

TODAY = '2026-07-16'

def test_enrichment_empty_to_filled_is_not_modified():
    # Anreicherung: routing/layover_ort leer -> gefuellt = KEINE Aenderung.
    old = [_day('2026-07-18', klass='Z72', routing=None, lay=None)]
    new = [_day('2026-07-18', klass='Z72', routing='FRA-JFK', lay='JFK')]
    d = A._compute_roster_diff(old, new, today=TODAY)
    assert d == []

def test_real_value_change_is_modified():
    old = [_day('2026-07-18', klass='Z72', routing='FRA-JFK', start='08:00')]
    new = [_day('2026-07-18', klass='Z72', routing='FRA-JFK', start='09:05')]
    d = A._compute_roster_diff(old, new, today=TODAY)
    assert len(d) == 1 and d[0]['kind'] == 'modified'

def test_klass_change_counts_even_empty_to_filled():
    # Tour wird Frei o.ae. — klass exakt verglichen.
    old = [_day('2026-07-18', klass=None)]
    new = [_day('2026-07-18', klass='FREI')]
    d = A._compute_roster_diff(old, new, today=TODAY)
    assert len(d) == 1 and d[0]['kind'] == 'modified'

def test_far_future_added_is_suppressed():
    # Neuer Monat veroeffentlicht: Tag 40 Tage entfernt = KEIN 'added'-Eintrag.
    old = [_day('2026-07-16', klass='Z72')]
    new = old + [_day('2026-08-25', klass='Z72', routing='FRA-MEX')]
    d = A._compute_roster_diff(old, new, today=TODAY)
    assert d == []

def test_near_added_is_reported():
    # Neuer Dienst uebermorgen = echtes, meldenswertes 'added'.
    old = [_day('2026-07-16', klass='Z72')]
    new = old + [_day('2026-07-18', klass='Z72', routing='FRA-MEX')]
    d = A._compute_roster_diff(old, new, today=TODAY)
    assert len(d) == 1 and d[0]['kind'] == 'added' and d[0]['datum'] == '2026-07-18'

def test_removed_still_reported():
    old = [_day('2026-07-18', klass='Z72', routing='FRA-JFK')]
    new = []
    d = A._compute_roster_diff(old, new, today=TODAY)
    assert len(d) == 1 and d[0]['kind'] == 'removed'

def test_semantically_identical_roster_yields_no_change():
    # Kern-Regression (Tibor 2026-07-17): ein Roster, das sich NUR in
    # Formatierung/Whitespace/Gross-Kleinschreibung unterscheidet, ist KEINE
    # Aenderung → ZERO Diffs (kein falscher „Dienstplan-Aenderung"-Push).
    old = [
        _day('2026-07-16', klass='Z76', routing='FRA-JFK', start='08:00',
             end='17:30', lay='JFK'),
        _day('2026-07-17', klass='Frei'),
    ]
    new = [
        # gleiche Substanz, andere Schreibweise/Whitespace/Case
        _day('2026-07-16', klass='z76', routing=' FRA - JFK ', start='08:00',
             end='17:30', lay='jfk'),
        _day('2026-07-17', klass='FREI'),
    ]
    d = A._compute_roster_diff(old, new, today=TODAY)
    assert d == []

def test_exact_same_roster_yields_no_change():
    # Byte-identisches Re-Sync (haeufigster Fall) → ZERO Diffs.
    r = [
        _day('2026-07-16', klass='Z76', routing='FRA-JFK', start='08:00',
             end='17:30', lay='JFK'),
        _day('2026-07-18', klass='Standby'),
    ]
    d = A._compute_roster_diff([dict(x) for x in r], [dict(x) for x in r], today=TODAY)
    assert d == []
