"""LH-MQTT-Push-Notifications (Engine A2) — rein offline: kein Netz, kein
Broker, kein Supabase. Blueprint-Logik läuft auf einer Mini-Flask-App (nur
lh_mqtt_bp), die Seams `_sector_rows`/`_rows_for_flight`/`_do_push`/`lh_flight_facts` werden
gemonkeypatcht. Topic-/Payload-Shapes sind die LIVE verifizierten
(Broker-Smoke-Test 2026-07-22)."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

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


def test_inbound_delay_pushes_before_feeder_departs(client, monkeypatch):
    # Der Zubringer verspätet sich schon VOR seinem Start → Frühwarnung an die
    # wartende Crew (est_dep-Event, Delay ≥15).
    from datetime import datetime as dt, timezone as tz
    now = dt.now(tz.utc)
    facts = dict(INBOUND_FACTS, dep_delay_min=35,
                 est_dep='2026-07-22T15:30:00+02:00',
                 sched_dep='2026-07-22T14:55:00+02:00')
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: dict(facts))
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: _rows([_layover_leg(now, tail='D-AIKP')]))
    monkeypatch.setattr(lh_mqtt, '_arr_board_rows', lambda *a, **k: [])
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda tok, title, body, data=None, idempotency_key=None:
                        pushes.append((title, body, data, idempotency_key)))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Estimated Departure',
                                     flight='LH123')).get_json()
    assert d['kind'] == 'est_dep' and d['pushed'] == 1
    title, body, data, key = pushes[0]
    assert 'verspätet sich' in title and 'LH400' in title
    assert 'D-AIKP (LH123) startet in MUC erst 15:30 (+35 min)' in body
    assert 'Ankunft in FRA ca. 14:30' in body
    assert data['type'] == 'inbound_delay'
    assert 'estdep:15:30' in key


def test_inbound_delay_small_is_silent(client, monkeypatch):
    facts = dict(INBOUND_FACTS, dep_delay_min=8,
                 est_dep='2026-07-22T15:03:00+02:00')
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: dict(facts))
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: pytest.fail('unter 15 min kein Station-Query'))
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda *a, **k: pytest.fail('+8 min pusht nicht'))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Estimated Departure',
                                     flight='LH123')).get_json()
    assert d['pushed'] == 0


def test_est_dep_serves_direct_crew_and_inbound_watchers(client, monkeypatch):
    # LH123 ist ROSTER-Flug von user0 UND Zubringer von user0s Kollegin (LH400
    # ab FRA) → beide Pushes aus EINEM Event. Topic-Datum = LOKALES Abflugdatum
    # des Sektors (Broker-Keying) — dynamisch aus `now`, sonst wird der Test
    # nach Mitternacht datumsabhängig rot (so geschehen am 2026-07-23).
    from datetime import datetime as dt, timezone as tz
    from zoneinfo import ZoneInfo
    now = dt.now(tz.utc)
    topic_date = now.astimezone(ZoneInfo('Europe/Berlin')).date().isoformat()
    facts = dict(INBOUND_FACTS, dep_delay_min=35,
                 est_dep='2026-07-22T15:30:00+02:00',
                 sched_dep='2026-07-22T14:55:00+02:00')
    direct_leg = {'flight': 'LH123', 'from': 'MUC', 'to': 'FRA',
                  'dep_iso': (now).isoformat()}
    pushes = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight',
                        lambda dates, c, n: _rows([direct_leg]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: dict(facts))
    monkeypatch.setattr(lh_mqtt, '_rows_from_station',
                        lambda dates, st: _rows([_layover_leg(now, tail='D-AIKP')]))
    monkeypatch.setattr(lh_mqtt, '_arr_board_rows', lambda *a, **k: [])
    monkeypatch.setattr(lh_mqtt, '_do_push',
                        lambda tok, title, body, data=None, idempotency_key=None:
                        pushes.append(data['type']))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Estimated Departure',
                                     flight='LH123',
                                     date=topic_date)).get_json()
    assert d['pushed'] == 2
    assert sorted(pushes) == ['flight_update', 'inbound_delay']


def test_inbound_topics_subscribe_feeder_flight(monkeypatch):
    from datetime import datetime as dt, timezone as tz, timedelta as td
    from zoneinfo import ZoneInfo
    now = dt.now(tz.utc)
    leg = _layover_leg(now)  # kein Roster-Tail → LH-autoritative Reg
    arr_local = now.astimezone(ZoneInfo('Europe/Berlin')) + td(hours=2)
    board = [{'airport': 'FRA#ARR', 'flight': 'LH123', 'reg': 'D-AIKP',
              'sched': None, 'esti': arr_local.strftime('%H:%M'),
              'date': arr_local.date().isoformat()}]
    # Die Topics-Rechnung löst Regs seit 2026-07-27 im BATCH auf (_legs_regs),
    # nicht mehr pro Leg — der Mock muss die echte Form spiegeln.
    monkeypatch.setattr(lh_mqtt, '_legs_regs',
                        lambda legs, dep_times=None, deadline=None:
                        {leg_key: 'D-AIKP' for leg_key in legs})
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


# ── Live-Activity-Fanout (P7-Verdrahtung 2026-07-27) ─────────────────────────
# Ersetzt den Tripwire-Platzhalter in tests/test_live_activity.py: der Hook
# `push_for_affected` ist jetzt in `lh_mqtt_event` angeschlossen — NUR für
# wirklich betroffene Crews (affected non-empty). Der Inbound-Watch-Pfad
# (Zubringer-Maschinen ohne Roster-Crew) bleibt Live-Activity-frei UND
# behält ausdrücklich KEIN Betroffenheits-Gate (Quota-Runden-Befund).

def test_event_triggers_live_activity_for_affected(client, monkeypatch):
    from blueprints import live_activity as LA
    calls = []
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight',
                        lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {
        'dep_delay_min': 35, 'est_dep': '2026-07-22T17:45:00+02:00',
        'sched_dep': '2026-07-22T17:10:00+02:00'})
    monkeypatch.setattr(lh_mqtt, '_do_push', lambda *a, **k: True)
    monkeypatch.setattr(LA, 'push_for_affected',
                        lambda affected, kind, flight_disp, topic_date,
                        facts=None: calls.append(
                            (affected, kind, flight_disp, topic_date, facts))
                        or 1)
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Estimated Departure')).get_json()
    assert d['la_sent'] == 1
    assert len(calls) == 1
    affected, kind, flight_disp, topic_date, facts = calls[0]
    assert kind == 'est_dep' and flight_disp == 'LH400'
    assert topic_date == '2026-07-22'
    assert [tok for tok, _s in affected] == ['user0']
    assert facts.get('dep_delay_min') == 35   # frische Fakten reisen mit


def test_inbound_path_never_calls_live_activity(client, monkeypatch):
    # Zubringer-Szenario: KEIN Roster trägt den Flug (affected leer), aber der
    # Inbound-Watch pusht. Live-Activity darf hier NICHT feuern — und der
    # Inbound-Pfad darf umgekehrt nie auf „nur wenn betroffen" gegated werden.
    from blueprints import live_activity as LA
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight', lambda dates, c, n: [])
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {})
    monkeypatch.setattr(lh_mqtt, '_push_inbound',
                        lambda kind, disp, date, facts=None: 1)
    monkeypatch.setattr(LA, 'push_for_affected',
                        lambda *a, **k: pytest.fail(
                            'Live-Activity ohne betroffene Crew verboten'))
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Departed')).get_json()
    assert d['kind'] == 'departed'
    assert d['pushed'] == 1        # Inbound-Watch lief trotz affected == []
    assert d['la_sent'] == 0


def test_departed_event_refreshes_facts_for_live_activity(client, monkeypatch):
    # Owner 2026-07-28 („arrival time was wrong the whole time"): beim
    # departed-Event lief vorher KEIN Fakten-Refresh → der Fanout fiel auf die
    # Roster-SOLL-Ankunft zurück. Jetzt reisen frische est_arr-Fakten mit.
    from blueprints import live_activity as LA
    seen = {}
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight',
                        lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: (seen.__setitem__('force', k.get('force')),
                                         {'est_arr': '2026-07-22T23:58:00+02:00'})[1])
    monkeypatch.setattr(lh_mqtt, '_push_inbound',
                        lambda kind, disp, date, facts=None: 0)
    monkeypatch.setattr(LA, 'push_for_affected',
                        lambda affected, kind, flight_disp, topic_date,
                        facts=None: (seen.__setitem__('facts', facts), 1)[1])
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('Departed')).get_json()
    assert d['kind'] == 'departed' and d['la_sent'] == 1
    assert seen['force'] is True
    assert seen['facts'].get('est_arr') == '2026-07-22T23:58:00+02:00'
    # departed erzeugt weiterhin KEINEN Alert-Push an die Direkt-Crew.
    assert d['pushed'] == 0


def test_est_arr_event_reaches_live_activity(client, monkeypatch):
    from blueprints import live_activity as LA
    calls = []
    lh_mqtt._facts_force_last.clear()
    monkeypatch.setattr(lh_mqtt, '_rows_for_flight',
                        lambda dates, c, n: _rows([LH400]))
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {
        'est_arr': '2026-07-22T23:41:00+02:00'})
    monkeypatch.setattr(LA, 'push_for_affected',
                        lambda affected, kind, flight_disp, topic_date,
                        facts=None: calls.append(kind) or 1)
    d = client.post('/api/internal/lh-mqtt/event',
                    json=_event_body('New Estimated Arrival')).get_json()
    assert d['kind'] == 'est_arr'
    assert d['la_sent'] == 1 and calls == ['est_arr']
    # est_arr pusht keinen Alert (nur die Lockscreen-Karte) — Owner-Regel
    # „keine Zeit-Pushes".
    assert d['pushed'] == 0


def test_est_arr_force_is_throttled_per_flight():
    # ACARS-ETA kann minütlich ticken; Force max. 1×/10 min pro Flug+Datum.
    lh_mqtt._facts_force_last.clear()
    assert lh_mqtt._facts_force_ok('LH454', '2026-07-28', now=1000.0) is True
    assert lh_mqtt._facts_force_ok('LH454', '2026-07-28', now=1300.0) is False
    # Anderer Flug ist unabhängig; nach Ablauf der Sperre wieder frei.
    assert lh_mqtt._facts_force_ok('LH455', '2026-07-28', now=1300.0) is True
    assert lh_mqtt._facts_force_ok('LH454', '2026-07-28', now=1601.0) is True


# ── Reg-Cache: Prozess-Memo → geteilter Cache → LH (Quota-Fix 2026-07-27) ────
# Hintergrund: `mqtt_leg_reg` war mit 73 % der grösste LH-Verbraucher, weil das
# Reg-Memo ein In-Process-dict war — 3× fragmentiert und bei jedem
# gunicorn-Recycle (~alle 37 min) weg. Diese Tests nageln die drei Eigenschaften
# fest, die das behoben haben.

@pytest.fixture(autouse=False)
def _clean_reg(monkeypatch):
    lh_mqtt._reg_memo.clear()
    monkeypatch.setattr(lh_mqtt, '_sb', lambda: None)   # kein Supabase im Test
    return lh_mqtt._reg_memo


def test_legs_regs_dedupes_same_flight_across_users(_clean_reg, monkeypatch):
    """Ein Flug mit N Crews darf EINEN LH-Call kosten, nicht N."""
    calls = []
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: calls.append(a) or {'reg': 'D-AIKP'})
    leg = ('LH400', '2026-07-22', 'FRA', 'JFK')
    out = lh_mqtt._legs_regs([leg, leg, leg, leg])
    assert out == {leg: 'D-AIKP'}
    assert len(calls) == 1


def test_legs_regs_memo_hit_costs_nothing(_clean_reg, monkeypatch):
    calls = []
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: calls.append(a) or {'reg': 'D-AIKP'})
    leg = ('LH400', '2026-07-22', 'FRA', 'JFK')
    lh_mqtt._legs_regs([leg])
    lh_mqtt._legs_regs([leg])
    assert len(calls) == 1


def test_lh_outage_is_not_cached_as_missing_reg(_clean_reg, monkeypatch):
    """LH-503/Throttle-Abweisung ist „wir wissen es nicht" — darf NICHT 30 min
    als „hat keine Reg" gelten. Vorher tat es das und verlängerte jeden
    LH-Ausfall um eine halbe Stunde."""
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {})
    import blueprints.lh_open_api as lho
    monkeypatch.setattr(lho, 'last_call_answered', lambda: False)
    leg = ('LH400', '2026-07-22', 'FRA', 'JFK')
    assert lh_mqtt._legs_regs([leg]) == {leg: None}
    key = lh_mqtt._reg_cache_key(*leg)
    expiry, _val = lh_mqtt._reg_memo[key]
    assert expiry - time.time() <= lh_mqtt._REG_UNKNOWN_TTL_S + 1


def test_real_no_reg_answer_is_cached_long(_clean_reg, monkeypatch):
    """Eine ECHTE Antwort ohne Reg (LH kennt den Flug, hat aber keine Maschine
    zugeteilt) darf sehr wohl negativ gecacht werden."""
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts', lambda *a, **k: {})
    import blueprints.lh_open_api as lho
    monkeypatch.setattr(lho, 'last_call_answered', lambda: True)
    leg = ('LH400', '2026-07-22', 'FRA', 'JFK')
    assert lh_mqtt._legs_regs([leg]) == {leg: None}
    key = lh_mqtt._reg_cache_key(*leg)
    expiry, _val = lh_mqtt._reg_memo[key]
    assert expiry - time.time() > lh_mqtt._REG_UNKNOWN_TTL_S + 1


def test_shared_cache_hit_skips_lh_entirely(monkeypatch):
    """Der Punkt der ganzen Übung: ein frisch recycelter Worker (leeres Memo)
    zieht die Regs aus dem geteilten Cache statt sie neu zu kaufen."""
    lh_mqtt._reg_memo.clear()
    calls = []
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: calls.append(a) or {'reg': 'X'})
    leg = ('LH400', '2026-07-22', 'FRA', 'JFK')
    key = lh_mqtt._reg_cache_key(*leg)
    monkeypatch.setattr(lh_mqtt, '_reg_cache_read', lambda keys: {key: 'D-AIKP'})
    monkeypatch.setattr(lh_mqtt, '_reg_cache_write', lambda items: None)
    monkeypatch.setattr(lh_mqtt, '_sb', lambda: object())
    assert lh_mqtt._legs_regs([leg]) == {leg: 'D-AIKP'}
    assert not calls


# ── Reg-TTL nach Abflugnähe + Gate-Abbruch (Quota-Runde 2 · 2026-07-27) ─────
# Messung nach dem Reg-Cache-Fix: `mqtt_leg_reg` blieb mit ~560–940 Calls/h der
# grösste Verbraucher, weil die FLACHE 3-h-TTL jedes der ~320 Legs im
# 17-h-Fenster ~5,7-mal neu kaufte. Und bei geschlossenem Gate versuchte JEDER
# Topic-Poll (alle 300 s) alle Legs erneut → 3.901 abgewiesene Versuche/h.

def test_reg_ttl_expires_exactly_at_the_recheck_before_departure():
    """Weit vor dem Abflug hält die Reg genau bis zum Gegencheck (Abflug −90
    min) — nicht kürzer (Verschwendung), nicht länger (Tailswap-blind)."""
    now = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    dep = now + timedelta(hours=6)
    ttl = lh_mqtt._reg_ttl(dep, now.timestamp())
    assert ttl == int(6 * 3600 - lh_mqtt._REG_RECHECK_LEAD_S)


def test_reg_ttl_is_final_inside_the_recheck_window():
    """Ab 90 min vor Abflug (und nach dem Abflug) ist die Reg final — genau
    hier lag der Grossteil der alten Wiederholungskäufe."""
    now = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
    for lead_min in (89, 0, -60):
        dep = now + timedelta(minutes=lead_min)
        assert lh_mqtt._reg_ttl(dep, now.timestamp()) == lh_mqtt._REG_FINAL_TTL_S


def test_reg_ttl_without_departure_time_keeps_the_old_flat_ttl():
    """Aufrufer ohne Abflugzeit verhalten sich unverändert."""
    assert lh_mqtt._reg_ttl(None, time.time()) == lh_mqtt._REG_TTL_S


def test_shut_gate_stops_the_batch_instead_of_hammering_it(_clean_reg,
                                                           monkeypatch):
    """Weist der EIGENE Throttle ab, gilt das für jeden weiteren Call dieser
    Stunde. Der Batch muss abbrechen — sonst feuert er alle ~320 Legs gegen
    dieselbe Wand (gemessen: 3.901 abgewiesene Versuche/h, kein einziger
    davon konnte je eine Reg liefern)."""
    calls = []
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(lh_mqtt, '_leg_reg_gate_shut', lambda: True)
    import blueprints.lh_open_api as lho
    monkeypatch.setattr(lho, 'last_call_answered', lambda: False)
    legs = [('LH40%d' % i, '2026-07-27', 'FRA', 'JFK') for i in range(5)]
    out = lh_mqtt._legs_regs(legs)
    assert out == {leg: None for leg in legs}
    assert len(calls) == 1          # nach dem ersten „Gate zu" kein Versuch mehr


def test_lh_outage_does_not_stop_the_batch(_clean_reg, monkeypatch):
    """Gegenprobe: ein LH-503 betrifft NUR diesen einen Flug — die restlichen
    Legs müssen weiter versucht werden (sonst wäre ein einzelner kaputter Flug
    ein Totalausfall des Inbound-Watch)."""
    calls = []
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(lh_mqtt, '_leg_reg_gate_shut', lambda: False)
    import blueprints.lh_open_api as lho
    monkeypatch.setattr(lho, 'last_call_answered', lambda: False)
    legs = [('LH40%d' % i, '2026-07-27', 'FRA', 'JFK') for i in range(5)]
    lh_mqtt._legs_regs(legs)
    assert len(calls) == 5


def test_unknown_hold_outlasts_the_topic_poll_interval():
    """Die Sperre nach einer Lücke MUSS länger sein als der Topic-Abgleich des
    Daemons (LH_MQTT_REFRESH_S, Default 300 s) — sonst versucht es der nächste
    Poll sofort wieder und der Sturm ist zurück."""
    assert lh_mqtt._REG_UNKNOWN_TTL_S > 300


def test_found_reg_is_shared_with_its_own_ttl(_clean_reg, monkeypatch):
    """Prozess-Memo und geteilter Cache müssen DIESELBE TTL bekommen."""
    written = []
    monkeypatch.setattr(lh_mqtt, 'lh_flight_facts',
                        lambda *a, **k: {'reg': 'D-AIKP'})
    monkeypatch.setattr(lh_mqtt, '_sb', lambda: object())
    monkeypatch.setattr(lh_mqtt, '_reg_cache_read', lambda keys: {})
    monkeypatch.setattr(lh_mqtt, '_reg_cache_write',
                        lambda items: written.extend(items))
    now = datetime.now(timezone.utc)
    leg = ('LH400', now.date().isoformat(), 'FRA', 'JFK')
    dep = now + timedelta(hours=8)
    lh_mqtt._legs_regs([leg], dep_times={leg: dep})
    assert len(written) == 1
    key, reg, ttl = written[0]
    assert (key, reg) == (lh_mqtt._reg_cache_key(*leg), 'D-AIKP')
    assert ttl == lh_mqtt._reg_ttl(dep, time.time())
    memo_expiry, _v = lh_mqtt._reg_memo[key]
    assert abs((memo_expiry - time.time()) - ttl) < 2


# ── Frische-Schranke (Birgit Münch, 2026-07-30) ─────────────────────────────
# Ihr Umlauf wurde gestrichen, ihre Server-Kopie stand vier Tage still — und der
# Fanout pushte weiter zu Flügen, aus denen sie rausgenommen war. Der Fanout
# darf „du bist auf diesem Flug" nur behaupten, solange der Beleg dafür frisch
# ist.

def _row(hours_ago, token='AT-X'):
    ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {'token': token, 'datum': '2026-07-30',
            'updated_at': ts.isoformat(), 'sectors': [{'flight': 'LH400'}]}


def test_frische_schranke_wirft_veraltete_zeilen_raus():
    """Birgits Fall: eine vier Tage alte Zeile ist kein Beleg mehr."""
    rows = [_row(2, 'frisch'), _row(96, 'birgit')]
    out = lh_mqtt._drop_stale_rows(rows)
    assert [r['token'] for r in out] == ['frisch']


def test_frische_schranke_laesst_server_gepflegte_zeilen_in_ruhe():
    """LH-verbundene User werden serverseitig nachgeladen (gemessen: max 9,2 h
    alt). Die Schranke bei 72 h darf sie NIE treffen — sonst verschluckt sie
    echte Verspätungs-Pushes."""
    rows = [_row(1), _row(9), _row(24), _row(71)]
    assert len(lh_mqtt._drop_stale_rows(rows)) == 4


def test_frische_schranke_ist_fail_open():
    """Ohne oder mit kaputtem `updated_at` bleibt die Zeile drin — lieber ein
    Push zu viel als eine echte Änderung still verschluckt."""
    rows = [{'token': 'A', 'sectors': []},
            {'token': 'B', 'updated_at': 'kein-datum', 'sectors': []}]
    assert len(lh_mqtt._drop_stale_rows(rows)) == 2
    assert lh_mqtt._drop_stale_rows(None) == []


def test_updated_at_wird_ueberhaupt_gelesen():
    """Die Schranke kann nur greifen, wenn das Select das Feld mitbringt."""
    assert 'updated_at' in lh_mqtt._SECTOR_SELECT
