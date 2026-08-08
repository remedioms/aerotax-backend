"""Per-friend privacy for sensitive roster states."""
import datetime as dt
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as A


OWNER = 'AT-OWNER-VISIBILITY'
FRIEND = 'AT-FRIEND-VISIBILITY'


def _headers(token=OWNER):
    return {'Authorization': f'Bearer {token}'}


def _direct_response(value):
    if isinstance(value, tuple):
        return value[0], value[1]
    return value, getattr(value, 'status_code', 200)


def test_sick_marker_detection_is_narrow():
    assert A._friend_day_is_sick({'klass': 'KRANK'})
    assert A._friend_day_is_sick({'marker': 'Sick leave'})
    assert A._friend_day_is_sick({'marker': 'KK'})
    assert not A._friend_day_is_sick({'klass': 'OFF', 'marker': 'Frei'})
    assert not A._friend_day_is_sick({'marker': 'Training'})


def test_visibility_defaults_to_shared_and_honors_explicit_false():
    assert A._friend_visibility_share_sick({}, FRIEND)
    profile = {'friend_visibility': {FRIEND: {'share_sick_status': False}}}
    assert not A._friend_visibility_share_sick(profile, FRIEND)
    assert A._friend_visibility_share_sick(profile, 'someone-else')


def test_settings_endpoint_persists_private_choice(monkeypatch):
    saved = {}
    monkeypatch.setattr(A, '_friends_load', lambda token: {'friends': [FRIEND]})
    monkeypatch.setattr(A, '_profile_load',
                        lambda token, fresh=False: {'token': token, 'profile': {}})
    monkeypatch.setattr(A, '_profile_save',
                        lambda token, profile, full_disk_payload=None:
                            saved.update(profile) is None)
    monkeypatch.setattr(A, '_profile_memo_invalidate', lambda token: None)
    monkeypatch.setattr(A, '_invalidate_friend_visibility_memos',
                        lambda owner, viewer: None)

    with A.app.test_request_context(
            f'/api/user/friend-visibility/{OWNER}/{FRIEND}', method='PUT',
            json={'share_sick_status': False}, headers=_headers()):
        response, status = _direct_response(
            A.friend_visibility_settings(OWNER, FRIEND))
    assert status == 200
    assert response.get_json()['share_sick_status'] is False
    assert saved['friend_visibility'][FRIEND]['share_sick_status'] is False


def test_settings_endpoint_rejects_non_friend(monkeypatch):
    monkeypatch.setattr(A, '_friends_load', lambda token: {'friends': []})
    with A.app.test_request_context(
            f'/api/user/friend-visibility/{OWNER}/{FRIEND}', method='PUT',
            json={'share_sick_status': False}, headers=_headers()):
        response, status = _direct_response(
            A.friend_visibility_settings(OWNER, FRIEND))
    assert status == 403


def test_friend_roster_omits_sick_day_for_excluded_viewer(monkeypatch):
    datum = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    A._FRIEND_ROSTER_MEMO.clear()
    monkeypatch.setattr(A, '_friends_load', lambda token: {'friends': [OWNER]})
    monkeypatch.setattr(A, '_maybe_refresh_calendar_feed', lambda *a, **k: None)
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda token: {})
    monkeypatch.setattr(A, '_ical_briefings_load', lambda token: {})
    monkeypatch.setattr(A, '_enrich_leg_delays', lambda *a, **k: None)
    monkeypatch.setattr(
        A, '_profile_load',
        lambda token, fresh=False: {
            'profile': {'friend_visibility': {
                FRIEND: {'share_sick_status': False}}}})
    A._store[OWNER] = {'result_data': {'_tage_detail': [{
        'datum': datum, 'klass': 'KRANK', 'marker': 'Krank',
        'reader_facts': {'start_time': '08:00', 'end_time': '17:00'},
    }]}}
    try:
        with A.app.test_request_context(
                f'/api/user/friend-roster/{FRIEND}/{OWNER}',
                headers=_headers(FRIEND)):
            response, status = _direct_response(
                A.get_friend_roster(FRIEND, OWNER))
        assert status == 200
        assert response.get_json()['days'] == []
    finally:
        A._store.pop(OWNER, None)
