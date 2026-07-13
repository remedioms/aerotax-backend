"""Crew-Hotel-Autorisierung (Owner 2026-07-13, Cross-Airline-Leak P0).

Regel (nicht verhandelbar): ein Crew-Hotel (layover-rec category 'sleep' MIT
author_airline) ist NUR sichtbar, wenn (1) das Profil des Betrachters dieselbe
Airline trägt UND (2) ein gültiger Kalender/Roster hängt. Fail-closed, keine
Airline mischt; generische Tipps (food/…) und 'sleep' ohne author_airline bleiben
crowdsourced sichtbar.
"""
import app


def _mock_viewer(monkeypatch, airline, has_cal):
    monkeypatch.setattr(app, '_profile_load', lambda t: {'profile': {'airline': airline}})
    monkeypatch.setattr(
        app, '_ical_briefings_load',
        lambda t: ({'2026-07-01': {'ical_imported_at': '2026-06-01T00:00:00'}} if has_cal else {}))


RECS = [
    {'id': 1, 'category': 'sleep', 'author_airline': 'Lufthansa', 'title': 'LH crew hotel'},
    {'id': 2, 'category': 'sleep', 'author_airline': 'Swiss', 'title': 'LX crew hotel'},
    {'id': 3, 'category': 'food', 'author_airline': 'Lufthansa', 'title': 'pizza tip'},
    {'id': 4, 'category': 'sleep', 'author_airline': '', 'title': 'generic hostel'},
]


def _ids(recs):
    return sorted(r['id'] for r in recs)


def test_lh_crew_with_calendar_sees_only_lh_crew_hotels(monkeypatch):
    _mock_viewer(monkeypatch, 'Lufthansa', True)
    # LH hotel(1) + generic tip(3) + un-airlined sleep(4); NOT the Swiss hotel(2)
    assert _ids(app._filter_crew_hotels(list(RECS), 'AT-x')) == [1, 3, 4]


def test_lh_crew_without_calendar_sees_no_crew_hotels(monkeypatch):
    _mock_viewer(monkeypatch, 'Lufthansa', False)
    # doppelt sicher: Airline allein reicht nicht → keine airline-getaggten Crew-Hotels
    assert _ids(app._filter_crew_hotels(list(RECS), 'AT-x')) == [3, 4]


def test_other_airline_never_sees_foreign_crew_hotels(monkeypatch):
    _mock_viewer(monkeypatch, 'Eurowings', True)
    # der gemeldete Leak: Eurowings darf LH/Swiss-Crewhotels NIE sehen
    assert _ids(app._filter_crew_hotels(list(RECS), 'AT-x')) == [3, 4]


def test_anonymous_sees_no_crew_hotels():
    # kein Token → ('', False) → fail-closed
    assert _ids(app._filter_crew_hotels(list(RECS), '')) == [3, 4]


def test_case_insensitive_airline_match(monkeypatch):
    _mock_viewer(monkeypatch, 'lufthansa', True)  # lower-case profile airline
    assert 1 in _ids(app._filter_crew_hotels(list(RECS), 'AT-x'))


def test_viewer_helper_fails_closed_on_error(monkeypatch):
    def boom(t):
        raise RuntimeError('db down')
    monkeypatch.setattr(app, '_profile_load', boom)
    monkeypatch.setattr(app, '_ical_briefings_load', boom)
    # Fehler → ('', False) → Crew-Hotels werden ausgeblendet (nie geleakt)
    assert app._viewer_airline_and_calendar('AT-x') == ('', False)
