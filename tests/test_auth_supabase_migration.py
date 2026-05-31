"""P0-Fix Tests: Auth-Persistenz in Supabase mit Disk-Fallback.

Verifiziert dass `_auth_load`/`_auth_save` korrekt zwischen Supabase und
Disk routet. Wichtig damit Accounts NICHT mehr bei Cloud-Run-Redeploys
verloren gehen.
"""
import sys
import os
import json
import tempfile
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


def _setup_temp_history_dir(monkeypatch_or_tmp):
    """Returns tempdir-path."""
    td = tempfile.mkdtemp(prefix='aerotax-auth-test-')
    return td


def test_auth_load_uses_supabase_when_available():
    """Wenn SB erreichbar + hat Daten → SB-Daten werden zurückgegeben (nicht disk)."""
    import app as A
    fake_sb_data = {'a@b.de': {'token': 'AT-X', 'password_hash': 'h1'}}
    with patch.object(A, '_auth_load_from_supabase', return_value=fake_sb_data):
        with patch.object(A, '_auth_load_from_disk', return_value={'OTHER@disk.de': {'token': 'AT-Y'}}):
            users = A._auth_load()
    assert users == fake_sb_data, "SB-Daten müssen Disk überschreiben"


def test_auth_load_falls_back_to_disk_when_sb_down():
    """Wenn SB unreachable → Disk-Daten als Fallback."""
    import app as A
    fake_disk_data = {'disk@user.de': {'token': 'AT-D', 'password_hash': 'h'}}
    with patch.object(A, '_auth_load_from_supabase', return_value=None):
        with patch.object(A, '_auth_load_from_disk', return_value=fake_disk_data):
            users = A._auth_load()
    assert users == fake_disk_data, "SB-down → Disk-Fallback erwartet"


def test_auth_load_lazy_migrates_disk_to_sb():
    """SB erreichbar aber leer, Disk hat Daten → einmalige Migration."""
    import app as A
    legacy_disk = {'legacy@user.de': {'token': 'AT-L', 'password_hash': 'h'}}
    save_calls = []
    with patch.object(A, '_auth_load_from_supabase', return_value={}):
        with patch.object(A, '_auth_load_from_disk', return_value=legacy_disk):
            with patch.object(A, '_auth_save_to_supabase',
                              side_effect=lambda d: save_calls.append(d) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    users = A._auth_load()
    assert users == legacy_disk, "Disk-Daten zurückgeben"
    assert save_calls == [legacy_disk], "Eine Migration-Save zur SB"


def test_auth_save_writes_both_disk_and_sb():
    """_auth_save schreibt disk (best-effort cache) + SB (primary)."""
    import app as A
    d = {'x@y.de': {'token': 'AT-Z', 'password_hash': 'h'}}
    disk_calls = []
    sb_calls = []
    with patch.object(A, '_atomic_write_json',
                      side_effect=lambda p, x: disk_calls.append((p, x))):
        with patch.object(A, '_auth_save_to_supabase',
                          side_effect=lambda d2: sb_calls.append(d2) or True):
            ok = A._auth_save(d)
    assert ok is True
    assert len(disk_calls) == 1, "Disk-Write erwartet"
    assert sb_calls == [d], "SB-Write erwartet"


def test_auth_save_succeeds_if_only_sb_works():
    """Wenn Disk-Write fehlschlägt aber SB OK → True (kein Daten-Verlust)."""
    import app as A
    d = {'x@y.de': {'token': 'AT-Z'}}
    with patch.object(A, '_atomic_write_json', side_effect=OSError('disk full')):
        with patch.object(A, '_auth_save_to_supabase', return_value=True):
            ok = A._auth_save(d)
    assert ok is True, "SB-Save reicht — kein Daten-Verlust"


def test_auth_save_fails_when_both_fail():
    """Wenn weder Disk noch SB → False (Caller muss wissen)."""
    import app as A
    d = {'x@y.de': {'token': 'AT-Z'}}
    with patch.object(A, '_atomic_write_json', side_effect=OSError('disk full')):
        with patch.object(A, '_auth_save_to_supabase', return_value=False):
            ok = A._auth_save(d)
    assert ok is False, "Beides fail → ehrlich False zurückgeben"


def test_auth_save_to_supabase_extracts_known_columns():
    """Known columns landen in eigenen Feldern, Rest in metadata."""
    import app as A
    captured_rows = []
    fake_table = MagicMock()
    fake_table.upsert.return_value.execute.return_value = MagicMock()

    def capture_upsert(rows, **kw):
        captured_rows.extend(rows)
        return fake_table

    fake_table.upsert.side_effect = lambda rows, **kw: (
        captured_rows.extend(rows) or fake_table
    )

    d = {
        'a@b.de': {
            'password_hash': 'hash123',
            'token': 'AT-T1',
            'apple_sub': 'apple-sub-1',
            'reset_token': 'rt-x',
            'unknown_field': 'goes-to-meta',
            'another_unknown': 42,
        }
    }
    with patch.object(A, 'SB_AVAILABLE', True):
        with patch.object(A, 'sb') as mock_sb:
            mock_sb.table.return_value.upsert.return_value.execute.return_value = MagicMock()
            # Capture the upsert payload
            real_upsert = mock_sb.table.return_value.upsert
            real_upsert.side_effect = lambda rows, **kw: (
                captured_rows.extend(rows) or MagicMock(execute=lambda: MagicMock())
            )
            A._auth_save_to_supabase(d)

    assert len(captured_rows) == 1
    row = captured_rows[0]
    assert row['email'] == 'a@b.de'
    assert row['password_hash'] == 'hash123'
    assert row['token'] == 'AT-T1'
    assert row['apple_sub'] == 'apple-sub-1'
    assert row['reset_token'] == 'rt-x'
    assert row['metadata']['unknown_field'] == 'goes-to-meta'
    assert row['metadata']['another_unknown'] == 42


if __name__ == '__main__':
    test_auth_load_uses_supabase_when_available()
    print("✓ test_auth_load_uses_supabase_when_available")
    test_auth_load_falls_back_to_disk_when_sb_down()
    print("✓ test_auth_load_falls_back_to_disk_when_sb_down")
    test_auth_load_lazy_migrates_disk_to_sb()
    print("✓ test_auth_load_lazy_migrates_disk_to_sb")
    test_auth_save_writes_both_disk_and_sb()
    print("✓ test_auth_save_writes_both_disk_and_sb")
    test_auth_save_succeeds_if_only_sb_works()
    print("✓ test_auth_save_succeeds_if_only_sb_works")
    test_auth_save_fails_when_both_fail()
    print("✓ test_auth_save_fails_when_both_fail")
    test_auth_save_to_supabase_extracts_known_columns()
    print("✓ test_auth_save_to_supabase_extracts_known_columns")
    print("\n7/7 OK — Auth-Persistenz korrekt zwischen SB und Disk geroutet.")
