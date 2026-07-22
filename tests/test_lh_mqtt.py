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


def test_event_gate_refreshes_facts_but_never_pushes(client, monkeypatch):
    # Owner 22.07.: „Gate ist egal" — Gate-Events refreshen nur die Fakten
    # (frisches Gate in der App), pushen aber nie.
    facts_calls = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight',
                        lambda dates, c, n: _rows([LH400], [dict(LH400)]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: facts_calls.append(k) or
                        {'gate': 'C16', 'terminal': '1'})
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pytest.fail('Gate pusht nie'))
    r = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Gate Information'))
    d = r.get_json()
    assert d['kind'] == 'gate' and d['users'] == 2 and d['pushed'] == 0
    assert facts_calls and facts_calls[0].get('force') is True


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


def test_event_departed_without_inbound_reg_pushes_nobody(client, monkeypatch):
    # Departed pusht die EIGENE Crew nie; ohne LH-Reg gibt es auch keinen
    # Inbound-Watch → 0 Pushes.
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {})
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: pytest.fail('ohne Reg kein Station-Query'))
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pytest.fail('Departed pusht die eigene Crew nie'))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Departed')).get_json()
    assert d['kind'] == 'departed' and d['pushed'] == 0


# ── Inbound-Watch (Zubringer-Maschine) ───────────────────────────────────────

def _layover_leg(now_utc, tail=None, flight='LH400', frm='FRA'):
    """Leg, das in 3h ab `frm` startet (dynamisch — _push_inbound rechnet mit
    der echten Uhr)."""
    from datetime import timedelta
    s = {'flight': flight, 'from': frm, 'to': 'JFK',
         'dep_iso': (now_utc + timedelta(hours=3)).isoformat()}
    if tail:
        s['tail'] = tail
    return s


INBOUND_FACTS = {'reg': 'D-AIKP', 'arr_iata': 'FRA', 'dep_iata': 'MUC',
                 'est_arr': '2026-07-22T14:30:00+02:00', 'arr_delay_min': 10}


def test_inbound_departed_pushes_layover_crew(client, monkeypatch):
    from datetime import datetime as dt, timezone as tz
    now = dt.now(tz.utc)
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: dict(INBOUND_FACTS))
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: _rows([_layover_leg(now, tail='D-AIKP')]))
    monkeypatch.setattr(lh_mqtt, '_arr_board_rows', lambda *a, **k: [])
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda tok, title, body, data=None, idempotency_key=None:
                        pushes.append((tok, title, body, data, idempotency_key)))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Departed', flight='LH123')).get_json()
    assert d['kind'] == 'departed' and d['pushed'] == 1
    tok, title, body, data, key = pushes[0]
    assert 'gestartet' in title and 'LH400' in title
    assert 'D-AIKP kommt als LH123 aus MUC' in body
    assert 'Ankunft in FRA ca. 14:30' in body and '(+10 min)' in body
    assert data['type'] == 'inbound_departure'
    assert data['inbound_flight'] == 'LH123' and data['flight'] == 'LH400'
    assert 'lhflup:inb:LH123' in key


def test_inbound_arrived_mentions_own_departure(client, monkeypatch):
    from datetime import datetime as dt, timezone as tz
    from zoneinfo import ZoneInfo
    now = dt.now(tz.utc)
    leg = _layover_leg(now, tail='D-AIKP')
    dep_local = (now.astimezone(ZoneInfo('Europe/Berlin')) +
                 __import__('datetime').timedelta(hours=3)).strftime('%H:%M')
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: dict(INBOUND_FACTS))
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: _rows([leg]))
    monkeypatch.setattr(lh_mqtt, '_arr_board_rows', lambda *a, **k: [])
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda tok, title, body, data=None, idempotency_key=None:
                        pushes.append((title, body, data)))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Arrived', flight='LH123')).get_json()
    assert d['pushed'] == 1
    title, body, data = pushes[0]
    assert 'gelandet' in title
    assert 'D-AIKP ist in FRA gelandet' in body
    assert f'dein LH400 geht um {dep_local}' in body
    assert data['type'] == 'inbound_arrival'


def test_inbound_early_rotation_is_filtered(client, monkeypatch):
    # Board kennt einen SPÄTEREN Zubringer (LH999) → das Event der früheren
    # Rotation (LH123) pusht nicht.
    from datetime import datetime as dt, timezone as tz
    from zoneinfo import ZoneInfo
    now = dt.now(tz.utc)
    arr_local = (now.astimezone(ZoneInfo('Europe/Berlin')) +
                 __import__('datetime').timedelta(hours=2))
    board = [{'airport': 'FRA#ARR', 'flight': 'LH999', 'reg': 'D-AIKP',
              'sched': arr_local.strftime('%H:%M'), 'esti': None,
              'date': arr_local.date().isoformat()}]
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: dict(INBOUND_FACTS))
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: _rows([_layover_leg(now, tail='D-AIKP')]))
    monkeypatch.setattr(lh_mqtt, '_arr_board_rows', lambda *a, **k: board)
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pytest.fail('frühe Rotation pusht nicht'))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Departed', flight='LH123')).get_json()
    assert d['pushed'] == 0


def test_inbound_reg_mismatch_no_push(client, monkeypatch):
    from datetime import datetime as dt, timezone as tz
    now = dt.now(tz.utc)
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: dict(INBOUND_FACTS))
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: _rows([_layover_leg(now, tail='D-AIXX')]))
    monkeypatch.setattr(lh_mqtt, '_arr_board_rows', lambda *a, **k: [])
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pytest.fail('fremde Maschine pusht nicht'))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Departed', flight='LH123')).get_json()
    assert d['pushed'] == 0


def test_inbound_topics_subscribe_feeder_flight(monkeypatch):
    from datetime import datetime as dt, timezone as tz, timedelta as td
    from zoneinfo import ZoneInfo
    now = dt.now(tz.utc)
    leg = _layover_leg(now)  # kein Roster-Tail → LH-autoritative Reg
    arr_local = now.astimezone(ZoneInfo('Europe/Berlin')) + td(hours=2)
    board = [{'airport': 'FRA#ARR', 'flight': 'LH123', 'reg': 'D-AIKP',
              'sched': None, 'esti': arr_local.strftime('%H:%M'),
              'date': arr_local.date().isoformat()}]
    monkeypatch.setattr(lh_mqtt, '_cached_leg_reg',
                        lambda *a, **k: 'D-AIKP')
    monkeypatch.setattr(lh_mqtt, '_arr_board_rows', lambda *a, **k: board)
    topics = lh_mqtt.inbound_topics_for_rows(_rows([leg]), now)
    d0 = arr_local.date()
    assert f'prd/FlightUpdate/LH/LH123/{d0.isoformat()}' in topics
    assert f'prd/FlightUpdate/LH/LH123/{(d0 - td(days=1)).isoformat()}' in topics


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
