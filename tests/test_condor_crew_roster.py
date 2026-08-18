"""Condor-Crewliste serverseitig (Owner-Entscheidung 2026-08-18).

Owner woertlich: „warum lokal.. dann ist es nicht wie bei LH sollte der
ausfallen haben wir es im backend und koennen so ihr den immer laden.. auch bei
app loeschung und neue installation und so.. angeben wer bei aero x als crew
ist etc etc hotel auch mit bewertungen und alles alles sicher gespeichert wie
bei LH im backend".

ALLE NAMEN UND PERSONALNUMMERN HIER SIND SYNTHETISCH. Es steht bewusst kein
echter Dienstplan im Repo — die Fixtures bilden nur das FORMAT nach.
"""
import pytest

import app as app_module


# Synthetisch: Namen frei erfunden, Nummern im Condor-Format (Ziffern + Buchstabe).
CONDOR_ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:personal-feed-100001A\r
DTSTART:20260901T060000Z\r
DTEND:20260901T101500Z\r
SUMMARY:DE1234 FRA-PMI\r
LOCATION:FRA - PMI\r
DESCRIPTION:CP 100001A MUSTER\\, ANNA (FRA)\\nFO 200002B BEISPIEL\\, BEN (FRA)\\nPU 300003C PROBE\\, CLARA (FRA)\\nHotel\\nTest Palace\\nTeststrasse 1\\n+34 900 000000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:personal-feed-100001A-2\r
DTSTART:20260902T050000Z\r
DTEND:20260902T090000Z\r
SUMMARY:DE5678 PMI-FRA\r
LOCATION:PMI - FRA\r
DESCRIPTION:CP 100001A MUSTER\\, ANNA (FRA)\\nST 400004D SCHULZ\\, DORA (FRA)\r
END:VEVENT\r
END:VCALENDAR\r
"""


# ── Parser ──────────────────────────────────────────────────────────────────

def test_parser_reads_role_name_and_staff_no_from_description():
    rows = app_module._condor_crew_parse_ics(CONDOR_ICS)

    assert [r['flight'] for r in rows] == ['DE1234', 'DE5678']
    first = rows[0]
    assert first['date'] == '2026-09-01'
    assert first['dep'] == 'FRA' and first['arr'] == 'PMI'
    assert first['crew'] == [
        {'role': 'CP', 'name': 'Anna Muster', 'staff_no': '100001A',
         'base': 'FRA'},
        {'role': 'FO', 'name': 'Ben Beispiel', 'staff_no': '200002B',
         'base': 'FRA'},
        {'role': 'PU', 'name': 'Clara Probe', 'staff_no': '300003C',
         'base': 'FRA'},
    ]


def test_parser_reads_no_hotel_address_or_phone():
    """Hotel-Adresse und Telefonnummer bleiben draussen (Regel seit 08.08.)."""
    rows = app_module._condor_crew_parse_ics(CONDOR_ICS)

    dumped = str(rows)
    for secret in ('Test Palace', 'Teststrasse', '900 000000', 'Hotel'):
        assert secret not in dumped


def test_parser_returns_nothing_for_already_sanitised_ics():
    """Neue App-Builds strippen DESCRIPTION auf dem Geraet — dann gibt es hier
    schlicht nichts zu lesen (die Zeile kommt ueber den Upload-Endpoint)."""
    clean = app_module._condor_ics_privacy_sanitize(CONDOR_ICS)

    assert app_module._condor_crew_parse_ics(clean) == []


def test_sanitizer_still_removes_everything_it_removed_before():
    """Der ICS-Sanitize bleibt UNVERAENDERT — Rohtext wird nie persistiert."""
    clean = app_module._condor_ics_privacy_sanitize(CONDOR_ICS)

    assert 'DESCRIPTION' not in clean
    assert 'SUMMARY:DE1234 FRA-PMI' in clean
    for secret in ('100001A', 'MUSTER', 'Test Palace', 'personal-feed'):
        assert secret not in clean


# ── Fremd-Eingabe (Geraete-Upload) ──────────────────────────────────────────

def test_upload_payload_sanitizer_drops_junk_and_keeps_shape():
    rows = app_module._condor_crew_sanitize_items([
        {'date': '2026-09-01', 'flight': 'de 1234', 'dep': 'fra', 'arr': 'pmi',
         'crew': [{'role': 'cp', 'name': 'MUSTER, ANNA',
                   'staff_no': '100001a', 'base': 'fra', 'evil': 'x'},
                  {'role': 'FO', 'name': '   '},
                  {'role': 'CP', 'name': 'MUSTER, ANNA', 'staff_no': '100001A'}]},
        {'date': 'kaputt', 'flight': 'DE1', 'crew': [{'role': 'CP', 'name': 'X'}]},
        {'date': '2026-09-03', 'flight': 'DE9', 'crew': []},
    ])

    assert rows == [{'date': '2026-09-01', 'flight': 'DE1234',
                     'dep': 'FRA', 'arr': 'PMI',
                     'crew': [{'role': 'CP', 'name': 'Anna Muster',
                               'staff_no': '100001A', 'base': 'FRA'}]}]


# ── Eigene Personalnummer lernen ────────────────────────────────────────────

def test_learns_own_condor_staff_no_from_own_crew_list(monkeypatch):
    saved = {}
    monkeypatch.setattr(app_module, '_profile_load',
                        lambda t: {'profile': {'name': 'Anna Muster',
                                               'airline': 'Condor'}})
    monkeypatch.setattr(
        app_module, '_profile_save',
        lambda t, p, full_disk_payload=None: saved.update(p) or True)

    rows = app_module._condor_crew_parse_ics(CONDOR_ICS)
    got = app_module._condor_learn_own_staff_no('AT-anna', rows)

    assert got == '100001A'
    assert saved['condor_staff_no'] == '100001A'


def test_learns_nothing_when_the_own_name_is_ambiguous(monkeypatch):
    """Zwei Nummern unter demselben Namen ⇒ NICHTS speichern. Eine falsche
    Personalnummer wuerde spaeter ein fremdes Profil als „auf AeroX" ausweisen."""
    saved = {}
    monkeypatch.setattr(app_module, '_profile_load',
                        lambda t: {'profile': {'name': 'Anna Muster'}})
    monkeypatch.setattr(
        app_module, '_profile_save',
        lambda t, p, full_disk_payload=None: saved.update(p) or True)

    rows = [{'date': '2026-09-01', 'flight': 'DE1', 'crew': [
        {'role': 'CP', 'name': 'Anna Muster', 'staff_no': '100001A'}]},
        {'date': '2026-09-02', 'flight': 'DE2', 'crew': [
            {'role': 'CP', 'name': 'Anna Muster', 'staff_no': '999999Z'}]}]

    assert app_module._condor_learn_own_staff_no('AT-anna', rows) is None
    assert saved == {}


# ── Gate + Auslieferung ─────────────────────────────────────────────────────

@pytest.fixture
def client():
    app_module.app.config.update(TESTING=True)
    return app_module.app.test_client()


def _condor_viewer(monkeypatch, *, airline='CONDOR', has_cal=True,
                   briefings=None):
    monkeypatch.setattr(app_module, '_viewer_airline_and_calendar',
                        lambda t: (airline, has_cal))
    monkeypatch.setattr(app_module, '_ical_briefings_load',
                        lambda t: briefings if briefings is not None else {})


_OWN_ROSTER = {'2026-09-01': {'ical_imported_at': '2026-08-18T10:00:00Z',
                              'ical_summary': 'DE1234 FRA-PMI',
                              'ical_sectors': [{'flight': 'DE1234'}]}}


def test_gate_allows_only_a_leg_from_the_own_roster(monkeypatch):
    _condor_viewer(monkeypatch, briefings=_OWN_ROSTER)

    assert app_module._condor_roster_has_leg('AT-a', '2026-09-01', 'DE1234')
    # Anderer Flug am selben Tag → zu.
    assert not app_module._condor_roster_has_leg('AT-a', '2026-09-01', 'DE9999')
    # Richtiger Flug, anderer Tag → zu.
    assert not app_module._condor_roster_has_leg('AT-a', '2026-09-02', 'DE1234')
    # Praefix darf nicht oeffnen (DE123 ist nicht DE1234).
    assert not app_module._condor_roster_has_leg('AT-a', '2026-09-01', 'DE123')


def test_gate_is_fail_closed_without_calendar_or_airline(monkeypatch):
    _condor_viewer(monkeypatch, has_cal=False, briefings=_OWN_ROSTER)
    assert not app_module._condor_roster_has_leg('AT-a', '2026-09-01', 'DE1234')

    _condor_viewer(monkeypatch, airline='LUFTHANSA', briefings=_OWN_ROSTER)
    assert not app_module._condor_roster_has_leg('AT-a', '2026-09-01', 'DE1234')

    _condor_viewer(monkeypatch, briefings={
        '2026-09-01': {'ical_sectors': [{'flight': 'DE1234'}]}})   # kein Import
    assert not app_module._condor_roster_has_leg('AT-a', '2026-09-01', 'DE1234')


def test_get_rejects_a_foreign_token_without_that_leg(client, monkeypatch):
    """Fremder Condor-Token OHNE den Flug: 403 — nicht etwa eine leere Liste,
    die wie „keine Crew" aussaehe."""
    _condor_viewer(monkeypatch, briefings={})
    monkeypatch.setattr(app_module, '_condor_crew_store_fetch',
                        lambda *a: pytest.fail('Store darf nie gelesen werden'))

    resp = client.get('/api/ax/condor/crew?date=2026-09-01&flight=DE1234',
                      headers={'Authorization': 'Bearer AT-fremd'})

    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'leg_not_in_own_roster'


def test_get_serves_crew_with_the_aerox_flag_for_the_own_leg(client, monkeypatch):
    _condor_viewer(monkeypatch, briefings=_OWN_ROSTER)
    monkeypatch.setattr(app_module, '_condor_crew_store_fetch',
                        lambda t, d, f: {
                            'flight_date': '2026-09-01', 'flight': 'DE1234',
                            'source': 'server_ics',
                            'updated_at': '2026-08-18T10:00:00Z',
                            'crew': [
                                {'role': 'CP', 'name': 'Anna Muster',
                                 'staff_no': '100001A'},
                                {'role': 'FO', 'name': 'Ben Beispiel',
                                 'staff_no': '200002B'}]})
    monkeypatch.setattr(app_module, '_condor_crew_aerox_matches',
                        lambda members: {'200002B': {
                            'token': 'AT-ben', 'name': 'Ben Beispiel',
                            'airline': 'Condor', 'homebase': 'FRA',
                            'position': 'FO', 'avatar_url': None}})

    resp = client.get('/api/ax/condor/crew?date=2026-09-01&flight=DE1234',
                      headers={'Authorization': 'Bearer AT-anna'})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['flight_date'] == '2026-09-01'
    assert body['source'] == 'server_ics'
    assert body['crew'] == [
        {'role': 'CP', 'name': 'Anna Muster'},
        {'role': 'FO', 'name': 'Ben Beispiel',
         'aerox': {'token': 'AT-ben', 'name': 'Ben Beispiel',
                   'airline': 'Condor', 'homebase': 'FRA',
                   'position': 'FO', 'avatar_url': None}},
    ]
    # Die Personalnummer ist Match-Kriterium, KEIN Anzeigewert.
    assert '100001A' not in str(body)
    assert '200002B' not in str(body)


def test_get_never_leaks_a_foreign_at_credential(client, monkeypatch):
    """Ein AT ist das Bearer-Credential (Memory „Token = Credential"). Die
    Crewliste liefert fremde Profile — der Pfad MUSS deshalb unter der
    AXU-Redaction stehen, genau wie die LH-Crewliste."""
    foreign = 'AT-0123456789ABCDEF'
    _condor_viewer(monkeypatch, briefings=_OWN_ROSTER)
    monkeypatch.setattr(app_module, '_condor_crew_store_fetch',
                        lambda t, d, f: {
                            'flight_date': '2026-09-01', 'flight': 'DE1234',
                            'source': 'server_ics', 'updated_at': None,
                            'crew': [{'role': 'FO', 'name': 'Ben Beispiel',
                                      'staff_no': '200002B'}]})
    monkeypatch.setattr(app_module, '_condor_crew_aerox_matches',
                        lambda members: {'200002B': {
                            'token': foreign, 'name': 'Ben Beispiel',
                            'airline': 'Condor', 'homebase': 'FRA',
                            'position': 'FO', 'avatar_url': None}})

    resp = client.get('/api/ax/condor/crew?date=2026-09-01&flight=DE1234',
                      headers={'Authorization': 'Bearer AT-anna'})

    raw = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert foreign not in raw
    assert app_module._PUBLIC_USER_REF_PREFIX in raw


def test_upload_stores_only_legs_from_the_own_roster(client, monkeypatch):
    stored = []
    _condor_viewer(monkeypatch, briefings=_OWN_ROSTER)
    monkeypatch.setattr(app_module, '_profile_load',
                        lambda t: {'profile': {'name': 'Anna Muster'}})
    monkeypatch.setattr(app_module, '_profile_save',
                        lambda t, p, full_disk_payload=None: True)
    monkeypatch.setattr(
        app_module, '_condor_crew_store_upsert',
        lambda token, rows, source: stored.extend(
            [dict(r, source=source) for r in rows]) or len(rows))

    resp = client.post('/api/ax/condor/crew/upload',
                       headers={'Authorization': 'Bearer AT-anna'},
                       json={'items': [
                           {'date': '2026-09-01', 'flight': 'DE1234',
                            'crew': [{'role': 'CP', 'name': 'MUSTER, ANNA',
                                      'staff_no': '100001A'}]},
                           {'date': '2026-09-05', 'flight': 'DE4321',
                            'crew': [{'role': 'CP', 'name': 'FREMD, FRANZ',
                                      'staff_no': '777777X'}]}]})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body['stored'] == 1 and body['rejected'] == 1
    assert body['own_staff_no_known'] is True
    assert [r['flight'] for r in stored] == ['DE1234']
    assert stored[0]['source'] == 'device_structured'


def test_upload_is_idempotent_per_token_date_flight(client, monkeypatch):
    """Zweimal dasselbe hochladen ⇒ dieselbe eine Zeile, kein Duplikat."""
    table = {}
    _condor_viewer(monkeypatch, briefings=_OWN_ROSTER)
    monkeypatch.setattr(app_module, '_profile_load', lambda t: {'profile': {}})
    monkeypatch.setattr(app_module, '_profile_save',
                        lambda t, p, full_disk_payload=None: True)

    def _upsert(token, rows, source):
        for r in rows:
            table[(token, r['date'], r['flight'])] = r
        return len(rows)

    monkeypatch.setattr(app_module, '_condor_crew_store_upsert', _upsert)

    payload = {'items': [{'date': '2026-09-01', 'flight': 'DE1234',
                          'crew': [{'role': 'CP', 'name': 'MUSTER, ANNA',
                                    'staff_no': '100001A'}]}]}
    for _ in range(2):
        resp = client.post('/api/ax/condor/crew/upload',
                           headers={'Authorization': 'Bearer AT-anna'},
                           json=payload)
        assert resp.status_code == 200

    assert list(table) == [('AT-anna', '2026-09-01', 'DE1234')]


def test_upload_rejects_a_non_condor_account(client, monkeypatch):
    _condor_viewer(monkeypatch, airline='LUFTHANSA', briefings=_OWN_ROSTER)

    resp = client.post('/api/ax/condor/crew/upload',
                       headers={'Authorization': 'Bearer AT-lh'},
                       json={'items': [{'date': '2026-09-01',
                                        'flight': 'DE1234',
                                        'crew': [{'role': 'CP',
                                                  'name': 'X, Y'}]}]})

    assert resp.status_code == 403
    assert resp.get_json()['error'] == 'not_condor_crew'


def test_upsert_dedupes_the_same_leg_within_one_statement(monkeypatch):
    """Ein Rotationsplan kann dieselbe Flugnummer am selben Tag zweimal nennen.
    Postgres bricht ein `ON CONFLICT DO UPDATE` mit doppeltem Key ab — hier
    muss also EINE Zeile rausgehen (die letzte)."""
    sent = {}

    class _Table:
        def upsert(self, payload, on_conflict=None):
            sent['payload'] = payload
            sent['on_conflict'] = on_conflict
            return self

        def execute(self):
            return type('R', (), {'data': []})()

    monkeypatch.setattr(app_module, 'SB_AVAILABLE', True)
    monkeypatch.setattr(app_module, 'sb',
                        type('SB', (), {'table': lambda self, name: _Table()})())

    rows = [
        {'date': '2026-09-01', 'flight': 'DE1234',
         'crew': [{'role': 'CP', 'name': 'Alt Wert'}]},
        {'date': '2026-09-01', 'flight': 'DE1234',
         'crew': [{'role': 'CP', 'name': 'Neu Wert'}]},
        {'date': '2026-09-02', 'flight': 'DE1234',
         'crew': [{'role': 'CP', 'name': 'Anna Muster'}]},
    ]
    stored = app_module._condor_crew_store_upsert('AT-anna', rows, 'server_ics')

    assert stored == 2
    assert sent['on_conflict'] == 'token,flight_date,flight'
    keys = [(r['flight_date'], r['flight']) for r in sent['payload']]
    assert keys == [('2026-09-01', 'DE1234'), ('2026-09-02', 'DE1234')]
    assert sent['payload'][0]['crew'][0]['name'] == 'Neu Wert'
    assert all(r['source'] == 'server_ics' for r in sent['payload'])


def test_ingest_from_raw_ics_stores_structured_rows(monkeypatch):
    """Server-Fetch-Weg (A): Rohtext wird STRUKTURIERT gelesen — und der Text
    selbst geht nirgendwo hin."""
    captured = {}
    monkeypatch.setattr(app_module, '_profile_load',
                        lambda t: {'profile': {'name': 'Anna Muster'}})
    monkeypatch.setattr(app_module, '_profile_save',
                        lambda t, p, full_disk_payload=None: True)
    monkeypatch.setattr(
        app_module, '_condor_crew_store_upsert',
        lambda token, rows, source: captured.update(
            {'rows': rows, 'source': source}) or len(rows))

    count = app_module._condor_crew_ingest('AT-anna', CONDOR_ICS, 'server_ics')

    assert count == 2
    assert captured['source'] == 'server_ics'
    assert captured['rows'][0]['crew'][0]['staff_no'] == '100001A'
