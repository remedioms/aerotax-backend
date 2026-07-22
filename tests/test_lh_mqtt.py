"""LH-MQTT-Push-Notifications (Engine A2) — rein offline: kein Netz, kein
Broker, kein Supabase. Blueprint-Logik läuft auf einer Mini-Flask-App (nur
lh_mqtt_bp), die Seams `_sector_rows`/`_rows_for_flight`/`_do_push`/`lh_flight_facts` werden
gemonkeypatcht. Topic-/Payload-Shapes sind die LIVE verifizierten
(Broker-Smoke-Test 2026-07-22)."""
import os
import sys
from datetime import datetime, timezone

import pytest
from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_mqtt
import lh_mqtt_daemon as daemon


NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _rows(*sector_lists):
    """Briefing-Rows-Fixture: je ein User-Token pro Sektor-Liste."""
    return [{'token': f'user{i}', 'datum': '2026-07-22', 'sectors': secs}
            for i, secs in enumerate(sector_lists)]


LH400 = {'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
         'dep_iso': '2026-07-22T15:10:00Z', 'arr_iso': '2026-07-22T23:35:00Z'}


@pytest.fixture
def client(monkeypatch):
    app = Flask(__name__)
    app.register_blueprint(lh_mqtt.lh_mqtt_bp)
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    lh_mqtt._topics_memo['ts'] = 0.0
    return app.test_client()


# ── pure Helpers ─────────────────────────────────────────────────────────────

def test_norm_flight():
    assert lh_mqtt._norm_flight('LH 0400') == ('LH', '400')
    assert lh_mqtt._norm_flight('4Y136') == ('4Y', '136')
    assert lh_mqtt._norm_flight('lh2015') == ('LH', '2015')
    assert lh_mqtt._norm_flight('XYZ') is None
    assert lh_mqtt._norm_flight('') is None
    assert lh_mqtt._norm_flight('LH0000') is None


def test_sector_topic_date_uses_local_departure_date():
    # FRA im Juli = UTC+2: 22:30Z ist LOKAL schon der 23. → Topic-Datum 23.
    late = dict(LH400, dep_iso='2026-07-22T22:30:00Z')
    assert lh_mqtt._sector_topic_dates(late) == ['2026-07-23']
    # 21:30Z = 23:30 lokal → bleibt der 22.
    evening = dict(LH400, dep_iso='2026-07-22T21:30:00Z')
    assert lh_mqtt._sector_topic_dates(evening) == ['2026-07-22']


def test_sector_topic_date_unknown_airport_covers_neighbors():
    s = dict(LH400)
    s['from'] = ''
    assert lh_mqtt._sector_topic_dates(s) == [
        '2026-07-21', '2026-07-22', '2026-07-23']


def test_topics_for_rows_filters_and_dedupes():
    ua = dict(LH400, flight='UA900')                      # nicht LH-Group
    far = dict(LH400, dep_iso='2026-07-26T15:10:00Z')     # außerhalb +48h
    rows = _rows([LH400, ua, far], [LH400])               # 2 User, gleicher Flug
    topics = lh_mqtt.topics_for_rows(rows, NOW)
    assert topics == ['prd/FlightUpdate/LH/LH400/2026-07-22']


def test_classify_message():
    assert lh_mqtt.classify_message('New Gate Information') == 'gate'
    assert lh_mqtt.classify_message('New Estimated Departure') == 'est_dep'
    assert lh_mqtt.classify_message('Departed') == 'departed'
    assert lh_mqtt.classify_message('Arrived') == 'arrived'
    assert lh_mqtt.classify_message('Flight Cancelled') == 'cancelled'
    assert lh_mqtt.classify_message('Diverted') == 'diverted'
    assert lh_mqtt.classify_message('Quantum Flux') == 'other'


def test_daemon_diff_topics():
    sub, unsub = daemon.diff_topics({'a', 'b'}, {'b', 'c'})
    assert sub == ['c'] and unsub == ['a']


def test_daemon_credentials_parse(monkeypatch):
    calls = []

    def fake_http(url, method='GET', data=None, headers=None, timeout=15):
        calls.append(url)
        if 'oauth/token' in url:
            return {'access_token': 'tok123'}
        return {'CertificateManagementResource': {'CertificateManagement': {
            'javaWebToken': 'jwt456', 'clientID': 'aerox_99',
            'endpoint': 'lhgopenapi.lufthansa.com'}}}

    monkeypatch.setattr(daemon, '_http_json', fake_http)
    monkeypatch.setattr(daemon, '_KEY', 'k')
    monkeypatch.setattr(daemon, '_SECRET', 's')
    cid, jwt, host = daemon.fetch_mqtt_credentials()
    assert (cid, jwt, host) == ('aerox_99', 'jwt456', 'lhgopenapi.lufthansa.com')
    # Cert-Manager-POST läuft über api.lufthansa.com (Doku nennt fälschlich
    # lhgopenapi — dort 401; live verifiziert 2026-07-22).
    assert any(u.startswith('https://api.lufthansa.com/v1/flightUpdate/'
                            'credentials/JWT/') for u in calls)


# ── Endpoints ────────────────────────────────────────────────────────────────

def _event_body(message, flight='LH400', date='2026-07-22'):
    return {'topic': f'prd/FlightUpdate/{flight[:2]}/{flight}/{date}',
            'payload': {'Update': {'Timestamp': '2026-07-22T12:48:58',
                                   'Message': message,
                                   'FlightNumber': flight,
                                   'ScheduledFlightDate': date},
                        'Meta': {'@Version': '1.0.0'}}}


def test_topics_endpoint(client, monkeypatch):
    # datums-agnostisch: der Endpoint rechnet mit der ECHTEN Uhr → Sektor
    # dynamisch 6h in die Zukunft legen und das lokale FRA-Datum erwarten.
    from datetime import timedelta
    from zoneinfo import ZoneInfo
    dep = datetime.now(timezone.utc) + timedelta(hours=6)
    sector = dict(LH400, dep_iso=dep.isoformat())
    expected_date = dep.astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()
    monkeypatch.setattr(lh_mqtt, '_sector_rows', lambda dates: _rows([sector]))
    r = client.get('/api/internal/lh-mqtt/topics')
    assert r.status_code == 200
    d = r.get_json()
    assert d['ok'] and d['count'] == 1
    assert d['topics'] == [f'prd/FlightUpdate/LH/LH400/{expected_date}']


def test_secret_gate(client, monkeypatch):
    monkeypatch.setenv('ADSB_POLL_SECRET', 'geheim')
    assert client.get('/api/internal/lh-mqtt/topics').status_code == 403
    r = client.get('/api/internal/lh-mqtt/topics',
                   headers={'X-Poll-Secret': 'geheim'})
    assert r.status_code == 200


def test_event_gate_change_pushes_all_affected(client, monkeypatch):
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight',
                        lambda dates, c, n: _rows([LH400], [dict(LH400)]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: {'gate': 'C16', 'terminal': '1'})
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda tok, title, body, data=None, idempotency_key=None:
                        pushes.append((tok, title, body, data, idempotency_key)))
    r = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Gate Information'))
    d = r.get_json()
    assert d['kind'] == 'gate' and d['users'] == 2 and d['pushed'] == 2
    tok, title, body, data, key = pushes[0]
    assert 'Gate-Änderung' in title and 'Gate C16' in body
    assert data['type'] == 'flight_update'
    # wertbasierter Dedupe-Key: gleiches Gate pusht nie doppelt
    assert 'gate:C16' in key and tok in key


def test_event_gate_without_fact_is_honest(client, monkeypatch):
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {})
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pushes.append(a))
    r = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Gate Information'))
    assert r.get_json()['pushed'] == 1
    assert 'Details in der App' in pushes[0][2]


def test_event_small_delay_no_push(client, monkeypatch):
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {
        'dep_delay_min': 5, 'est_dep': '2026-07-22T17:15:00+02:00',
        'sched_dep': '2026-07-22T17:10:00+02:00'})
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pytest.fail('kein Push bei +5 min'))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Estimated Departure')).get_json()
    assert d['kind'] == 'est_dep' and d['pushed'] == 0


def test_event_real_delay_pushes(client, monkeypatch):
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {
        'dep_delay_min': 35, 'est_dep': '2026-07-22T17:45:00+02:00',
        'sched_dep': '2026-07-22T17:10:00+02:00'})
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda tok, title, body, **k: pushes.append(body))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Estimated Departure')).get_json()
    assert d['pushed'] == 1
    assert '17:45' in pushes[0] and 'statt 17:10' in pushes[0]
    assert '(+35 min)' in pushes[0]


def test_event_cancelled_pushes(client, monkeypatch):
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {})
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda tok, title, body, **k: pushes.append(title))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Flight Cancelled')).get_json()
    assert d['kind'] == 'cancelled' and d['pushed'] == 1
    assert 'annulliert' in pushes[0]


def test_event_departed_refreshes_nothing_and_pushes_nobody(client, monkeypatch):
    facts_calls = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: facts_calls.append(a) or {})
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pytest.fail('Departed pusht nie'))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Departed')).get_json()
    assert d['kind'] == 'departed' and d['pushed'] == 0
    assert not facts_calls  # kein Budget-Verbrauch ohne Push-Anlass


def test_event_no_affected_users_no_facts_call(client, monkeypatch):
    facts_calls = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: facts_calls.append(a) or {})
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Gate Information')).get_json()
    assert d['users'] == 0 and d['pushed'] == 0 and not facts_calls


def test_event_bad_topic_rejected(client):
    r = client.post('/api/internal/lh-mqtt/event',
                    json={'topic': 'kaputt', 'payload': {}})
    assert r.status_code == 400


def test_status_endpoint(client):
    r = client.get('/api/lh/mqtt/status')
    d = r.get_json()
    assert r.status_code == 200 and d['ok'] and 'events' in d


def test_iter_sectors_accepts_legacy_raw_event_shape():
    rows = [{'token': 'u1',
             'raw_event': {'ical_sectors': [dict(LH400)]}}]
    assert [t for t, _ in lh_mqtt._iter_sectors(rows)] == ['u1']


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._start = 0

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def filter(self, *_a, **_k):
        return self

    def range(self, start, end):
        self._start, self._end = start, end
        return self

    def execute(self):
        class R:
            pass
        r = R()
        r.data = self._rows[self._start:self._end + 1]
        return r


def test_sector_rows_paginates_past_postgrest_1000_cap(monkeypatch):
    # 2026-07-22 live: 3682 Rows im 4-Tage-Fenster — ohne range() fehlten
    # ~73% der User. Fake-Client mit 2500 Rows → alle kommen an.
    all_rows = [{'token': f'u{i}', 'sectors': []} for i in range(2500)]

    class _FakeSB:
        def table(self, *_a):
            return _FakeQuery(all_rows)

    monkeypatch.setattr(lh_mqtt, '_sb', lambda: _FakeSB())
    got = lh_mqtt._sector_rows(['2026-07-22'])
    assert len(got) == 2500
