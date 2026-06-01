"""P0 Worker-P1 Tests: Wall/Forum/DM-Persistenz in Supabase.

Verifiziert dass die Social-Loaders/Savers korrekt zwischen Supabase (primary)
und Disk (fallback/cache) routen — analog `test_auth_supabase_migration` &
`test_user_data_supabase_migration`. Wichtig damit Wall-Posts/Forum-Threads/
DM-Messages NICHT mehr bei Cloud-Run-Redeploys verloren gehen.

Test-Block-Layout (mindestens 4 pro Tabelle):
  - WALL-POSTS:    4 Tests (load_sb, load_disk_fallback, save_both, lazy_migrate)
  - FORUM-THREADS: 4 Tests (load_sb, load_disk_fallback, save_both, lazy_migrate)
  - FORUM-REPLIES: 4 Tests (load_sb, load_disk_fallback, save_both, lazy_migrate)
  - DM-MESSAGES:   4 Tests (load_sb, load_disk_fallback, save_both, lazy_migrate)
"""
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


# ════════════════════════════════════════════════════════════════════
# WALL-POSTS
# ════════════════════════════════════════════════════════════════════

def test_wall_posts_load_uses_supabase_when_available():
    """SB-Hit → SB-Daten zurueck; Disk wird ignoriert."""
    import app as A
    sb_posts = [{'id': 'p1', 'author_token': 'AT-X', 'ts': 1.0, 'text': 'hi'}]
    with patch.object(A, '_wall_posts_load_from_supabase', return_value=sb_posts):
        with patch.object(A, '_wall_load_posts_from_disk',
                          return_value=[{'id': 'stale', 'ts': 0}]):
            posts = A._wall_load_posts()
    assert posts == sb_posts, "SB-Posts muessen Disk ueberschreiben"


def test_wall_posts_load_falls_back_to_disk_when_sb_down():
    """SB-Ausfall (None) → Disk-Fallback."""
    import app as A
    disk_posts = [{'id': 'd1', 'author_token': 'AT-D', 'ts': 1.0, 'text': 'disk'}]
    with patch.object(A, '_wall_posts_load_from_supabase', return_value=None):
        with patch.object(A, '_wall_load_posts_from_disk', return_value=disk_posts):
            posts = A._wall_load_posts()
    assert posts == disk_posts, "SB-down → Disk-Fallback"


def test_wall_posts_save_writes_both_sb_and_disk():
    """save schreibt zu SB + Disk (analog _auth_save)."""
    import app as A
    posts = [{'id': 'p1', 'author_token': 'AT-X', 'ts': 1.0, 'text': 'hi'}]
    sb_calls = []
    disk_calls = []
    with patch.object(A, '_wall_posts_save_to_supabase',
                      side_effect=lambda p: sb_calls.append(p) or True):
        with patch.object(A, '_atomic_write_json',
                          side_effect=lambda p, d, **kw: disk_calls.append((p, d))):
            A._wall_save_posts(posts)
    assert sb_calls == [posts], "SB-Save erwartet"
    assert len(disk_calls) == 1, "Disk-Write erwartet"


def test_wall_posts_lazy_migrates_disk_to_sb():
    """SB erreichbar aber leer + Disk hat Daten → einmalige Migration."""
    import app as A
    disk_posts = [{'id': 'legacy', 'author_token': 'AT-L', 'ts': 1.0, 'text': 'old'}]
    save_calls = []
    with patch.object(A, '_wall_posts_load_from_supabase', return_value=[]):
        with patch.object(A, '_wall_load_posts_from_disk', return_value=disk_posts):
            with patch.object(A, '_wall_posts_save_to_supabase',
                              side_effect=lambda p: save_calls.append(p) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    posts = A._wall_load_posts()
    assert posts == disk_posts
    assert save_calls == [disk_posts], "Eine Migration-Save zur SB"


def test_wall_posts_save_to_supabase_text_to_body_column():
    """Disk-Feld 'text' muss in SB-Column 'body' landen — sonst sieht der
    SB-Lesepfad nach Migration leere Posts."""
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
            ok = A._wall_posts_save_to_supabase([{
                'id': 'p1', 'author_token': 'AT-T1', 'ts': 1.5,
                'text': 'Layover ZRH', 'image_url': '/api/wall/image/x/y.jpg',
                'hashtags': ['zrh'], 'like_count': 3, 'comment_count': 1,
                'author_name': 'Maria', 'author_short': 'AT-T1XYZ',
            }])
    assert ok is True
    assert len(captured) == 1
    row = captured[0]
    assert row['id'] == 'p1'
    assert row['body'] == 'Layover ZRH', "text→body Mapping fehlt"
    assert row['hashtags'] == ['zrh']
    assert row['like_count'] == 3
    # author_name + author_short sind keine Spalten → metadata
    assert row['metadata']['author_name'] == 'Maria'
    assert row['metadata']['author_short'] == 'AT-T1XYZ'


# ════════════════════════════════════════════════════════════════════
# FORUM-THREADS
# ════════════════════════════════════════════════════════════════════

def test_forum_threads_load_uses_supabase_when_available():
    """SB-Hit → SB-Daten zurueck."""
    import app as A
    sb_threads = [{'id': 't1', 'category_id': 'cabin', 'ts': 1.0,
                   'title': 'Hi', 'author_token': 'AT-X'}]
    with patch.object(A, '_forum_threads_load_from_supabase', return_value=sb_threads):
        with patch.object(A, '_forum_threads_load_from_disk',
                          return_value=[{'id': 'stale'}]):
            threads = A._forum_load_threads()
    assert threads == sb_threads


def test_forum_threads_load_falls_back_to_disk_when_sb_down():
    import app as A
    disk_threads = [{'id': 'd1', 'category_id': 'pay', 'ts': 1.0,
                     'title': 'Disk', 'author_token': 'AT-D'}]
    with patch.object(A, '_forum_threads_load_from_supabase', return_value=None):
        with patch.object(A, '_forum_threads_load_from_disk', return_value=disk_threads):
            threads = A._forum_load_threads()
    assert threads == disk_threads


def test_forum_threads_save_writes_both_sb_and_disk():
    import app as A
    threads = [{'id': 't1', 'category_id': 'cabin', 'ts': 1.0,
                'title': 'Hi', 'author_token': 'AT-X'}]
    sb_calls = []
    disk_calls = []
    with patch.object(A, '_forum_threads_save_to_supabase',
                      side_effect=lambda t: sb_calls.append(t) or True):
        with patch.object(A, '_atomic_write_json',
                          side_effect=lambda p, d, **kw: disk_calls.append((p, d))):
            A._forum_save_threads(threads)
    assert sb_calls == [threads]
    assert len(disk_calls) == 1


def test_forum_threads_lazy_migrates_disk_to_sb():
    import app as A
    disk_threads = [{'id': 'legacy', 'category_id': 'general', 'ts': 0.5,
                     'title': 'Old', 'author_token': 'AT-L', 'created_ts': 0.5}]
    save_calls = []
    with patch.object(A, '_forum_threads_load_from_supabase', return_value=[]):
        with patch.object(A, '_forum_threads_load_from_disk', return_value=disk_threads):
            with patch.object(A, '_forum_threads_save_to_supabase',
                              side_effect=lambda t: save_calls.append(t) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    threads = A._forum_load_threads()
    assert threads == disk_threads
    assert save_calls == [disk_threads]


def test_forum_threads_save_to_supabase_maps_created_ts_to_ts():
    """Legacy Disk-Feld 'created_ts' (kein 'ts' gesetzt) muss in SB-Column 'ts' landen."""
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
            A._forum_threads_save_to_supabase([{
                'id': 'tl1', 'category_id': 'pay', 'author_token': 'AT-T1',
                'title': 'Geld', 'body': 'Was', 'created_ts': 12.5,
                'hashtags': ['geld'], 'like_count': 0, 'reply_count': 0,
                'author_name': 'Max',
            }])
    assert len(captured) == 1
    row = captured[0]
    assert row['ts'] == 12.5, "created_ts→ts Mapping fehlt"
    assert row['body'] == 'Was'
    assert row['metadata']['author_name'] == 'Max'


# ════════════════════════════════════════════════════════════════════
# FORUM-REPLIES
# ════════════════════════════════════════════════════════════════════

def test_forum_replies_load_uses_supabase_when_available():
    import app as A
    sb_replies = [{'id': 'r1', 'thread_id': 'TX', 'ts': 2.0,
                   'body': 'reply', 'author_token': 'AT-X'}]
    with patch.object(A, '_forum_replies_load_from_supabase', return_value=sb_replies):
        with patch.object(A, '_forum_replies_load_from_disk',
                          return_value=[{'id': 'stale'}]):
            replies = A._forum_load_replies('TX')
    assert replies == sb_replies


def test_forum_replies_load_falls_back_to_disk_when_sb_down():
    import app as A
    disk_replies = [{'id': 'rd1', 'thread_id': 'TX', 'ts': 2.0,
                     'body': 'disk-reply', 'author_token': 'AT-D'}]
    with patch.object(A, '_forum_replies_load_from_supabase', return_value=None):
        with patch.object(A, '_forum_replies_load_from_disk', return_value=disk_replies):
            replies = A._forum_load_replies('TX')
    assert replies == disk_replies


def test_forum_replies_save_writes_both_sb_and_disk():
    import app as A
    replies = [{'id': 'r1', 'thread_id': 'TX', 'ts': 2.0,
                'body': 'r', 'author_token': 'AT-X'}]
    sb_calls = []
    disk_calls = []
    with patch.object(A, '_forum_replies_save_to_supabase',
                      side_effect=lambda tid, r: sb_calls.append((tid, r)) or True):
        with patch.object(A, '_atomic_write_json',
                          side_effect=lambda p, d, **kw: disk_calls.append((p, d))):
            A._forum_save_replies('TX', replies)
    assert sb_calls == [('TX', replies)]
    assert len(disk_calls) == 1


def test_forum_replies_lazy_migrates_disk_to_sb():
    import app as A
    disk_replies = [{'id': 'rL1', 'thread_id': 'TX', 'ts': 2.0,
                     'body': 'old', 'author_token': 'AT-L'}]
    save_calls = []
    with patch.object(A, '_forum_replies_load_from_supabase', return_value=[]):
        with patch.object(A, '_forum_replies_load_from_disk', return_value=disk_replies):
            with patch.object(A, '_forum_replies_save_to_supabase',
                              side_effect=lambda tid, r: save_calls.append((tid, r)) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    replies = A._forum_load_replies('TX')
    assert replies == disk_replies
    assert save_calls == [('TX', disk_replies)]


# ════════════════════════════════════════════════════════════════════
# DM-MESSAGES
# ════════════════════════════════════════════════════════════════════

def test_dm_messages_load_uses_supabase_when_available():
    import app as A
    sb_msgs = [{'id': 'm1', 'channel_id': 'dm__a__b', 'ts': 1.0,
                'text': 'hi', 'author_token': 'AT-X…'}]
    with patch.object(A, '_dm_messages_load_from_supabase', return_value=sb_msgs):
        with patch.object(A, '_dm_load_messages_from_disk',
                          return_value=[{'id': 'stale'}]):
            msgs = A._dm_load_messages('dm__a__b')
    assert msgs == sb_msgs


def test_dm_messages_load_falls_back_to_disk_when_sb_down():
    import app as A
    disk_msgs = [{'id': 'md1', 'channel_id': 'dm__a__b', 'ts': 1.0,
                  'text': 'disk', 'author_token': 'AT-D…'}]
    with patch.object(A, '_dm_messages_load_from_supabase', return_value=None):
        with patch.object(A, '_dm_load_messages_from_disk', return_value=disk_msgs):
            msgs = A._dm_load_messages('dm__a__b')
    assert msgs == disk_msgs


def test_dm_messages_save_writes_both_sb_and_disk():
    import app as A
    msgs = [{'id': 'm1', 'channel_id': 'dm__a__b', 'ts': 1.0,
             'text': 'hi', 'author_token': 'AT-X…'}]
    sb_calls = []
    disk_calls = []
    with patch.object(A, '_dm_messages_save_to_supabase',
                      side_effect=lambda ch, m: sb_calls.append((ch, m)) or True):
        with patch.object(A, '_atomic_write_json',
                          side_effect=lambda p, d, **kw: disk_calls.append((p, d))):
            A._dm_save_messages('dm__a__b', msgs)
    assert sb_calls == [('dm__a__b', msgs)]
    assert len(disk_calls) == 1


def test_dm_messages_lazy_migrates_disk_to_sb():
    import app as A
    disk_msgs = [{'id': 'mL1', 'channel_id': 'dm__a__b', 'ts': 1.0,
                  'text': 'old', 'author_token': 'AT-L…'}]
    save_calls = []
    with patch.object(A, '_dm_messages_load_from_supabase', return_value=[]):
        with patch.object(A, '_dm_load_messages_from_disk', return_value=disk_msgs):
            with patch.object(A, '_dm_messages_save_to_supabase',
                              side_effect=lambda ch, m: save_calls.append((ch, m)) or True):
                with patch.object(A, 'SB_AVAILABLE', True):
                    msgs = A._dm_load_messages('dm__a__b')
    assert msgs == disk_msgs
    assert save_calls == [('dm__a__b', disk_msgs)]


def test_dm_messages_save_to_supabase_text_to_body_column():
    """Disk-Feld 'text' muss in SB-Column 'body' landen + channel_id wird forciert."""
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
            A._dm_messages_save_to_supabase('dm__a__b', [{
                'id': 'm1', 'author_token': 'AT-T1…', 'ts': 5.0,
                'text': 'Layover sehen wir uns?', 'iso': '2026-06-01T10:00',
            }])
    assert len(captured) == 1
    row = captured[0]
    assert row['id'] == 'm1'
    assert row['channel_id'] == 'dm__a__b', "channel_id muss aus Argument kommen"
    assert row['body'] == 'Layover sehen wir uns?', "text→body Mapping"
    assert row['metadata']['iso'] == '2026-06-01T10:00'


# ════════════════════════════════════════════════════════════════════
# RUN
# ════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    tests = [
        # wall
        test_wall_posts_load_uses_supabase_when_available,
        test_wall_posts_load_falls_back_to_disk_when_sb_down,
        test_wall_posts_save_writes_both_sb_and_disk,
        test_wall_posts_lazy_migrates_disk_to_sb,
        test_wall_posts_save_to_supabase_text_to_body_column,
        # forum-threads
        test_forum_threads_load_uses_supabase_when_available,
        test_forum_threads_load_falls_back_to_disk_when_sb_down,
        test_forum_threads_save_writes_both_sb_and_disk,
        test_forum_threads_lazy_migrates_disk_to_sb,
        test_forum_threads_save_to_supabase_maps_created_ts_to_ts,
        # forum-replies
        test_forum_replies_load_uses_supabase_when_available,
        test_forum_replies_load_falls_back_to_disk_when_sb_down,
        test_forum_replies_save_writes_both_sb_and_disk,
        test_forum_replies_lazy_migrates_disk_to_sb,
        # dm
        test_dm_messages_load_uses_supabase_when_available,
        test_dm_messages_load_falls_back_to_disk_when_sb_down,
        test_dm_messages_save_writes_both_sb_and_disk,
        test_dm_messages_lazy_migrates_disk_to_sb,
        test_dm_messages_save_to_supabase_text_to_body_column,
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
    print(f"\n{passed}/{len(tests)} OK — Social-Persistenz SB+Disk korrekt geroutet.")
    sys.exit(0 if failed == 0 else 1)
