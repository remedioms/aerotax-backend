"""P0-Fix Phase 2 Tests: User-Daten (Profile/Friends/Push-Tokens) in Supabase.

Verifiziert dass Profile/Friends/Push-Persistenz korrekt zwischen Supabase
(primary) und Disk (fallback/cache) routet — analog `test_auth_supabase_migration`.
Wichtig damit User-Daten NICHT mehr bei Cloud-Run-Redeploys verloren gehen.

Test-Block-Layout:
  - PROFILE: 6 Tests (load_sb, load_disk_fallback, lazy_migration,
             save_both, save_only_sb, known_cols_extraction)
  - FRIENDS: 6 Tests (load_sb_with_in/out, load_disk_fallback,
             lazy_migration, save_both, save_sb_only, pending_vs_accepted)
  - PUSH:    6 Tests (load_sb, load_disk_fallback, lazy_migration,
             save_both, save_sb_only, legacy_field_mapping push_token→expo_token)
"""
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ════════════════════════════════════════════════════════════════════
# PROFILE
# ════════════════════════════════════════════════════════════════════

def test_profile_load_uses_supabase_when_available():
    """SB-Hit liefert SB-Daten (im 'profile'-Subkey), Disk wird gemerged für
    Side-Keys (subscription, crew_aircraft, …)."""
    import app as A
    sb_prof = {'name': 'Miguel', 'homebase': 'FRA', 'employers': []}
    disk_full = {
        'token': 'AT-X', 'profile': {'name': 'STALE'},
        'subscription': {'tier': 'premium'},
    }
    with patch.object(A, '_profile_load_from_supabase', return_value=sb_prof):
        with patch.object(A, '_profile_load_from_disk', return_value=disk_full):
            data = A._profile_load('AT-X')
    assert data['profile'] == sb_prof, "SB muss Disk-profile-Subkey ueberschreiben"
    assert data.get('subscription') == {'tier': 'premium'}, "Side-Key bleibt"


def test_profile_load_falls_back_to_disk_when_sb_down():
    """SB-down (None aus load_from_sb) → kompletter disk-payload zurueck."""
    import app as A
    disk_full = {'token': 'AT-D', 'profile': {'name': 'Disk-User'}}
    with patch.object(A, '_profile_load_from_supabase', return_value=None):
        with patch.object(A, '_profile_load_from_disk', return_value=disk_full):
            with patch.object(A, 'SB_AVAILABLE', False):
                data = A._profile_load('AT-D')
    assert data == disk_full, "SB-down → Disk-Fallback"


def test_profile_load_lazy_migrates_disk_to_sb():
    """SB erreichbar aber Row-fehlt, Disk hat Daten → einmalige Migration."""
    import app as A
    disk_full = {'token': 'AT-L', 'profile': {'name': 'Legacy', 'homebase': 'BER'}}
    save_calls = []
    with patch.object(A, '_profile_load_from_supabase', return_value=None):
        with patch.object(A, '_profile_load_from_disk', return_value=disk_full):
            with patch.object(A, '_profile_save_to_supabase',
                              side_effect=lambda t, p: save_calls.append((t, p)) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    data = A._profile_load('AT-L')
    assert data == disk_full
    assert save_calls == [('AT-L', disk_full['profile'])], "Lazy-Migration → 1 SB-write"


def test_profile_save_writes_both_disk_and_sb():
    """save schreibt zu SB + Disk (analog _auth_save)."""
    import app as A
    profile = {'name': 'X', 'homebase': 'MUC'}
    sb_calls = []
    disk_calls = []
    with patch.object(A, '_profile_save_to_supabase',
                      side_effect=lambda t, p: sb_calls.append((t, p)) or True):
        with patch.object(A, '_atomic_write_json',
                          side_effect=lambda p, d: disk_calls.append((p, d))):
            ok = A._profile_save('AT-Z', profile)
    assert ok is True
    assert sb_calls == [('AT-Z', profile)]
    assert len(disk_calls) == 1


def test_profile_save_succeeds_if_only_sb_works():
    """Disk-Fail aber SB-OK → True (kein Daten-Verlust)."""
    import app as A
    with patch.object(A, '_profile_save_to_supabase', return_value=True):
        with patch.object(A, '_atomic_write_json', side_effect=OSError('readonly')):
            ok = A._profile_save('AT-Z', {'name': 'x'})
    assert ok is True


def test_profile_save_to_supabase_extracts_known_columns():
    """Known columns landen in eigenen Feldern, share_location/current_city
    in metadata (sind nicht in der Tabellen-Whitelist)."""
    import app as A
    captured = []
    fake_table = MagicMock()
    fake_table.upsert.side_effect = lambda rows, **kw: (
        captured.extend(rows if isinstance(rows, list) else [rows])
        or MagicMock(execute=lambda: MagicMock())
    )
    with patch.object(A, 'SB_AVAILABLE', True):
        with patch.object(A, 'sb') as mock_sb:
            mock_sb.table.return_value = fake_table
            A._profile_save_to_supabase('AT-T1', {
                'name': 'Miguel', 'homebase': 'FRA', 'airline': 'LH',
                'employers': [{'name': 'LH'}],
                'share_location': True, 'current_city': 'Bangkok',
                'unknown_field': 'goes-to-meta',
            })
    assert len(captured) == 1
    row = captured[0]
    assert row['token'] == 'AT-T1'
    assert row['name'] == 'Miguel'
    assert row['homebase'] == 'FRA'
    assert row['employers'] == [{'name': 'LH'}]
    assert row['metadata']['share_location'] is True
    assert row['metadata']['current_city'] == 'Bangkok'
    assert row['metadata']['unknown_field'] == 'goes-to-meta'


# ════════════════════════════════════════════════════════════════════
# FRIENDS
# ════════════════════════════════════════════════════════════════════

def test_friends_load_uses_supabase_when_available():
    """SB-Hit liefert friends-Liste, requests_in/out separat."""
    import app as A
    sb_data = {
        'token': 'AT-X',
        'friends': ['AT-A', 'AT-B'],
        'requests_out': ['AT-C'],
        'requests_in': ['AT-D'],
    }
    with patch.object(A, '_friends_load_from_supabase', return_value=sb_data):
        with patch.object(A, '_friends_load_from_disk',
                          return_value={'token': 'AT-X', 'friends': [],
                                        'groups': [{'id': 'g1', 'name': 'G1'}]}):
            data = A._friends_load('AT-X')
    assert data['friends'] == ['AT-A', 'AT-B']
    assert data['requests_out'] == ['AT-C']
    assert data['requests_in'] == ['AT-D']
    # groups (disk-only) muss mit-gemerged werden
    assert data.get('groups') == [{'id': 'g1', 'name': 'G1'}]


def test_friends_load_falls_back_to_disk_when_sb_down():
    """SB-down → komplette Disk-Datei zurueck."""
    import app as A
    disk_data = {'token': 'AT-D', 'friends': ['AT-1']}
    with patch.object(A, '_friends_load_from_supabase', return_value=None):
        with patch.object(A, '_friends_load_from_disk', return_value=disk_data):
            with patch.object(A, 'SB_AVAILABLE', False):
                data = A._friends_load('AT-D')
    assert data == disk_data


def test_friends_load_lazy_migrates_disk_to_sb():
    """SB up, kein SB-Hit, Disk hat Friends → migration triggert."""
    import app as A
    disk_data = {'token': 'AT-L', 'friends': ['AT-X', 'AT-Y']}
    save_calls = []
    with patch.object(A, '_friends_load_from_supabase', return_value=None):
        with patch.object(A, '_friends_load_from_disk', return_value=disk_data):
            with patch.object(A, '_friends_save_to_supabase',
                              side_effect=lambda t, d: save_calls.append((t, d)) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    data = A._friends_load('AT-L')
    assert data == disk_data
    assert save_calls == [('AT-L', disk_data)]


def test_friends_save_writes_both_disk_and_sb():
    """save → SB + Disk."""
    import app as A
    payload = {'token': 'AT-Z', 'friends': ['AT-1']}
    sb_calls = []
    disk_calls = []
    with patch.object(A, '_friends_save_to_supabase',
                      side_effect=lambda t, d: sb_calls.append((t, d)) or True):
        with patch.object(A, '_atomic_write_json',
                          side_effect=lambda p, d: disk_calls.append((p, d))):
            ok = A._friends_save('AT-Z', payload)
    assert ok is True
    assert sb_calls == [('AT-Z', payload)]
    assert len(disk_calls) == 1


def test_friends_save_succeeds_if_only_sb_works():
    """Disk-Fail aber SB-OK → True."""
    import app as A
    with patch.object(A, '_friends_save_to_supabase', return_value=True):
        with patch.object(A, '_atomic_write_json', side_effect=OSError('readonly')):
            ok = A._friends_save('AT-Z', {'friends': ['AT-1']})
    assert ok is True


def test_friends_save_to_supabase_writes_accepted_and_pending_rows():
    """friends → status=accepted, requests_out → status=pending. requests_in
    werden NICHT vom Owner geschrieben (gehoeren dem anderen Owner).
    Delete-all-by-owner zuerst → dann insert/upsert."""
    import app as A
    delete_calls = []
    upsert_calls = []

    class FakeTable:
        def delete(self):
            return self
        def upsert(self, rows, **kw):
            upsert_calls.extend(rows if isinstance(rows, list) else [rows])
            return self
        def eq(self, *a, **kw):
            return self
        def execute(self):
            return MagicMock(data=[])

    # Track delete by wrapping
    real_table_factory = []

    class FakeTableTracked(FakeTable):
        def delete(self):
            delete_calls.append('deleted')
            return self
    fake_table = FakeTableTracked()

    with patch.object(A, 'SB_AVAILABLE', True):
        with patch.object(A, 'sb') as mock_sb:
            mock_sb.table.return_value = fake_table
            ok = A._friends_save_to_supabase('AT-OWN', {
                'friends': ['AT-A', 'AT-B'],
                'requests_out': ['AT-C'],
                'requests_in': ['AT-D'],  # darf NICHT in rows
            })
    assert ok is True
    assert 'deleted' in delete_calls
    statuses = {r['friend_token']: r['status'] for r in upsert_calls}
    assert statuses == {'AT-A': 'accepted', 'AT-B': 'accepted', 'AT-C': 'pending'}
    # requests_in nicht im upsert
    assert 'AT-D' not in statuses


# ════════════════════════════════════════════════════════════════════
# PUSH
# ════════════════════════════════════════════════════════════════════

def test_push_load_uses_supabase_when_available():
    """SB-Hit liefert legacy-shape (push_token statt expo_token)."""
    import app as A
    sb_reg = {
        'token': 'AT-X', 'push_token': 'ExponentPushToken[abc]',
        'apns_token': '', 'platform': 'expo', 'device_id': 'dev-1',
    }
    with patch.object(A, '_push_load_from_supabase', return_value=sb_reg):
        with patch.object(A, '_push_load_from_disk',
                          return_value={'token': 'AT-X',
                                        'push_token': 'STALE'}):
            data = A._push_load('AT-X')
    assert data == sb_reg


def test_push_load_falls_back_to_disk_when_sb_down():
    """SB-down → Disk-File zurueck."""
    import app as A
    disk_reg = {'token': 'AT-D', 'push_token': 'expo-old', 'apns_token': ''}
    with patch.object(A, '_push_load_from_supabase', return_value=None):
        with patch.object(A, '_push_load_from_disk', return_value=disk_reg):
            with patch.object(A, 'SB_AVAILABLE', False):
                data = A._push_load('AT-D')
    assert data == disk_reg


def test_push_load_lazy_migrates_disk_to_sb():
    """SB up, kein SB-Hit, Disk hat push_token → migration."""
    import app as A
    disk_reg = {'token': 'AT-L', 'push_token': 'expo-tok', 'apns_token': ''}
    save_calls = []
    with patch.object(A, '_push_load_from_supabase', return_value=None):
        with patch.object(A, '_push_load_from_disk', return_value=disk_reg):
            with patch.object(A, '_push_save_to_supabase',
                              side_effect=lambda t, r: save_calls.append((t, r)) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    data = A._push_load('AT-L')
    assert data == disk_reg
    assert save_calls == [('AT-L', disk_reg)]


def test_push_save_writes_both_disk_and_sb():
    """save → SB + Disk."""
    import app as A
    reg = {'token': 'AT-Z', 'push_token': 'expo-x', 'apns_token': 'a-y'}
    sb_calls = []
    disk_calls = []
    with patch.object(A, '_push_save_to_supabase',
                      side_effect=lambda t, r: sb_calls.append((t, r)) or True):
        with patch.object(A, '_atomic_write_json',
                          side_effect=lambda p, d: disk_calls.append((p, d))):
            ok = A._push_save('AT-Z', reg)
    assert ok is True
    assert sb_calls == [('AT-Z', reg)]
    assert len(disk_calls) == 1


def test_push_save_succeeds_if_only_sb_works():
    """Disk-Fail aber SB-OK → True."""
    import app as A
    with patch.object(A, '_push_save_to_supabase', return_value=True):
        with patch.object(A, '_atomic_write_json', side_effect=OSError('readonly')):
            ok = A._push_save('AT-Z', {'push_token': 'x'})
    assert ok is True


def test_push_save_to_supabase_maps_legacy_push_token_to_expo_column():
    """Legacy-Disk-Feld 'push_token' muss in SB-Column 'expo_token' landen.
    Sonst kommt der Token nach Migration nicht mehr beim Empfänger an."""
    import app as A
    captured = []
    fake_table = MagicMock()
    fake_table.upsert.side_effect = lambda rows, **kw: (
        captured.extend(rows if isinstance(rows, list) else [rows])
        or MagicMock(execute=lambda: MagicMock())
    )
    with patch.object(A, 'SB_AVAILABLE', True):
        with patch.object(A, 'sb') as mock_sb:
            mock_sb.table.return_value = fake_table
            A._push_save_to_supabase('AT-T1', {
                'token': 'AT-T1',
                'push_token': 'ExponentPushToken[xyz]',
                'apns_token': 'a-token-123',
                'platform': 'ios',
                'bundle_id': 'de.aerosteuer.aeris',
                'device_id': 'iphone-1',
                'registered_at': '2026-05-31T10:00:00',
            })
    assert len(captured) == 1
    row = captured[0]
    assert row['user_token'] == 'AT-T1'
    assert row['expo_token'] == 'ExponentPushToken[xyz]'
    assert row['apns_token'] == 'a-token-123'
    assert row['platform'] == 'ios'
    assert row['device_id'] == 'iphone-1'
    # bundle_id + registered_at landen in metadata (kein column)
    assert row['metadata']['bundle_id'] == 'de.aerosteuer.aeris'
    assert row['metadata']['registered_at'] == '2026-05-31T10:00:00'


# ════════════════════════════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    tests = [
        # profile
        test_profile_load_uses_supabase_when_available,
        test_profile_load_falls_back_to_disk_when_sb_down,
        test_profile_load_lazy_migrates_disk_to_sb,
        test_profile_save_writes_both_disk_and_sb,
        test_profile_save_succeeds_if_only_sb_works,
        test_profile_save_to_supabase_extracts_known_columns,
        # friends
        test_friends_load_uses_supabase_when_available,
        test_friends_load_falls_back_to_disk_when_sb_down,
        test_friends_load_lazy_migrates_disk_to_sb,
        test_friends_save_writes_both_disk_and_sb,
        test_friends_save_succeeds_if_only_sb_works,
        test_friends_save_to_supabase_writes_accepted_and_pending_rows,
        # push
        test_push_load_uses_supabase_when_available,
        test_push_load_falls_back_to_disk_when_sb_down,
        test_push_load_lazy_migrates_disk_to_sb,
        test_push_save_writes_both_disk_and_sb,
        test_push_save_succeeds_if_only_sb_works,
        test_push_save_to_supabase_maps_legacy_push_token_to_expo_column,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed}/{len(tests)} OK — User-Data-Persistenz SB+Disk korrekt geroutet.")
    sys.exit(0 if failed == 0 else 1)
