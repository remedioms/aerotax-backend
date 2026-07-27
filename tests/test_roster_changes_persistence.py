"""Roster-Verlauf (pending/history) Supabase-first + Disk-Fallback (2026-07-27).

Vorher lag der Verlauf NUR im ungemounteten Container-Layer → jeder Deploy
wischte pending + history („Push bekommen, aber keine Änderung gefunden",
Julia). Jetzt: Write-Through nach SB + Disk, Read SB-first, Lazy-Migration
des Disk-Bestands. Fehlt die Tabelle/SB, degradiert alles auf Disk-only —
Endpoint-Shape unverändert (kein Client-Update nötig).
"""
import json
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import app as A

TOK = 'AT-TEST-RC-PERSIST'


def test_read_prefers_supabase_over_missing_disk(tmp_path):
    """Deploy-Überleben: Disk ist weg (frischer Container), SB liefert den
    Verlauf → GET zeigt ihn weiterhin."""
    sb_payload = {'pending': [{'datum': '2026-08-01', 'kind': 'modified',
                               'status': 'pending'}], 'history': []}
    with patch.object(A, '_roster_changes_path',
                      return_value=str(tmp_path / 'missing.json')), \
         patch.object(A, '_sb_roster_changes_load', return_value=sb_payload), \
         patch.object(A, '_validate_token', return_value=True):
        c = A.app.test_client()
        r = c.get(f'/api/user/roster-changes/{TOK}',
                  headers={'Authorization': f'Bearer {TOK}'})
    assert r.status_code == 200
    body = r.get_json()
    assert len(body['pending']) == 1
    assert body['pending'][0]['datum'] == '2026-08-01'


def test_disk_fallback_lazy_migrates_to_supabase(tmp_path):
    """SB kennt den User noch nicht, Disk hat Bestand → Read liefert Disk UND
    hebt ihn einmalig nach SB (überlebt damit den NÄCHSTEN Deploy)."""
    cp = tmp_path / 'rc.json'
    disk = {'pending': [], 'history': [{'datum': '2026-07-20',
                                        'status': 'accepted'}]}
    cp.write_text(json.dumps(disk))
    lifted = []
    with patch.object(A, '_roster_changes_path', return_value=str(cp)), \
         patch.object(A, '_sb_roster_changes_load', return_value=None), \
         patch.object(A, '_sb_roster_changes_upsert',
                      side_effect=lambda t, d: lifted.append((t, d)) or True):
        data = A._roster_changes_read(TOK)
    assert data == disk
    assert lifted and lifted[0][0] == TOK and lifted[0][1] == disk


def test_save_writes_through_to_sb_and_disk_and_caps_history(tmp_path):
    cp = tmp_path / 'rc.json'
    upserts = []
    big = {'pending': [],
           'history': [{'i': i} for i in range(A._ROSTER_CHANGES_HISTORY_CAP + 40)]}
    with patch.object(A, '_roster_changes_path', return_value=str(cp)), \
         patch.object(A, '_sb_roster_changes_upsert',
                      side_effect=lambda t, d: upserts.append(d) or True):
        ok = A._roster_changes_save(TOK, big)
    assert ok is True
    on_disk = json.loads(cp.read_text())
    assert len(on_disk['history']) == A._ROSTER_CHANGES_HISTORY_CAP
    assert upserts and len(upserts[0]['history']) == A._ROSTER_CHANGES_HISTORY_CAP


def test_save_survives_sb_down_via_disk(tmp_path):
    """SB weg (Tabelle fehlt/down) → Disk allein reicht, kein Hard-Fail —
    exakt das Verhalten von vor der Migration."""
    cp = tmp_path / 'rc.json'
    with patch.object(A, '_roster_changes_path', return_value=str(cp)), \
         patch.object(A, '_sb_roster_changes_upsert', return_value=False):
        ok = A._roster_changes_save(TOK, {'pending': [], 'history': []})
    assert ok is True and cp.exists()


def test_decide_reads_sb_when_disk_gone(tmp_path):
    """Der Lockscreen-Button-Flow (decide) findet den pending Change auch nach
    einem Deploy (Disk weg, SB hat ihn) — vorher: 404 no_changes."""
    sb_payload = {'pending': [{'datum': '2026-08-02', 'kind': 'modified',
                               'status': 'pending'}], 'history': []}
    saved = []
    with patch.object(A, '_roster_changes_path',
                      return_value=str(tmp_path / 'missing.json')), \
         patch.object(A, '_sb_roster_changes_load', return_value=sb_payload), \
         patch.object(A, '_sb_roster_changes_upsert',
                      side_effect=lambda t, d: saved.append(d) or True), \
         patch.object(A, '_rc_decide_update_baseline',
                      return_value=None) if hasattr(A, '_rc_decide_update_baseline') \
             else patch.object(A, '_roster_snapshot_save', return_value=True), \
         patch.object(A, '_validate_token', return_value=True):
        c = A.app.test_client()
        r = c.post(f'/api/user/roster-changes/{TOK}/decide',
                   json={'datum': '2026-08-02', 'decision': 'accept'},
                   headers={'Authorization': f'Bearer {TOK}'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True
    assert saved and saved[-1]['pending'] == []
    assert saved[-1]['history'][-1]['status'] == 'accepted'
