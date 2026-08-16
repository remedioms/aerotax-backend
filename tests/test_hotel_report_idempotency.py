"""Hotel ratings survive client retries without duplicate community rows."""

from flask import Flask

from blueprints import hotel_rooms_blueprint as rooms


def _client():
    app = Flask(__name__)
    app.register_blueprint(rooms.hotel_rooms_bp)
    return app.test_client()


def test_supabase_insert_path_executes(monkeypatch):
    calls = []

    class Query:
        def insert(self, row):
            calls.append(row)
            return self

        def execute(self):
            return object()

    class Supabase:
        def table(self, name):
            assert name == 'hotel_room_reports'
            return Query()

    monkeypatch.setattr(rooms, '_sb_client', lambda: (Supabase(), True))
    assert rooms._sb_insert_report({'id': 'report-1'}) is True
    assert calls == [{'id': 'report-1'}]


def test_existing_client_report_id_bypasses_rate_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(rooms, '_USER_HISTORY_DIR', str(tmp_path))
    existing = {
        'id': 'server-id', 'reported_by_token': 'AT-user',
        'client_report_id': 'client_report_123', 'hotel_name': 'Hotel Test',
        'overall_rating': 5, 'upvote_count': 0,
    }
    monkeypatch.setattr(rooms, '_sb_report_by_client_id',
                        lambda *_args: existing)
    monkeypatch.setattr(
        rooms, '_rate_limited',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('idempotent retry must not consume quota')))
    response = _client().post('/api/hotel-rooms/AT-user/report', json={
        'client_report_id': 'client_report_123',
    })
    assert response.status_code == 200
    assert response.get_json()['idempotent'] is True
    assert response.get_json()['report']['id'] == 'server-id'


def test_same_client_report_id_creates_one_disk_row(monkeypatch, tmp_path):
    monkeypatch.setattr(rooms, '_USER_HISTORY_DIR', str(tmp_path))
    monkeypatch.setattr(rooms, '_sb_report_by_client_id',
                        lambda *_args: None)
    monkeypatch.setattr(rooms, '_sb_insert_report', lambda _row: False)
    monkeypatch.setattr(rooms, '_rate_limited',
                        lambda *_args, **_kwargs: False)
    payload = {'client_report_id': 'client_report_456',
               'hotel_name': 'Hotel Test', 'hotel_iata': 'FRA',
               'overall_rating': 4}
    client = _client()
    first = client.post('/api/hotel-rooms/AT-user/report', json=payload)
    second = client.post('/api/hotel-rooms/AT-user/report', json=payload)
    assert first.status_code == second.status_code == 200
    assert first.get_json()['report']['id'] == second.get_json()['report']['id']
    assert second.get_json()['idempotent'] is True
    assert len(rooms._disk_load(rooms._DISK_REPORTS)) == 1
