"""Crew+Maschine pro Flugtag: Persistenz in der DB statt auf Container-Disk.

Befund 2026-08-08: `POST /api/user/crew-aircraft/<token>/<datum>` nahm
Crew-Namen + Funktion + Maschine entgegen, schrieb sie aber per rohem
`json.dump` direkt in `profile_<token>.json` — an `_profile_save` VORBEI.
Die Daten erreichten Supabase nie und waren nach jedem Redeploy weg (gleiche
Fehlerklasse wie der Flugbuch-Historien-Verlust).

Fix: Store liegt in `user_profiles.metadata.crew_aircraft` und wird über
`_profile_sidekey_set` → `_profile_save` geschrieben (Vorbild `flight-notes`).
Der alte Top-Level-Disk-Key wird beim ersten Zugriff einmalig hochgezogen
(newest-wins).

Abgedeckt:
  - Schreiben landet in der DB (nicht nur auf Disk)
  - Lesen findet es wieder
  - Redeploy-Simulation: Container-Datei weg → nichts verloren
  - Warm-Migration des Legacy-Container-Stands (idempotent, newest-wins)
  - Owner-only: fremdes Token kommt weder lesend noch schreibend ran
  - Personalnummern werden verworfen
"""
import json
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import app as A

TOK = 'AT-TEST-CREWAC-OWNER'
OTHER = 'AT-TEST-CREWAC-FREMD'


def _valid_token(_t=None):
    return A._TokenValidationResult(A._TokenValidationState.VALID,
                                    'crewac@aerox.test')


class _FakeStore:
    """Minimaler Ersatz für Supabase + Container-Disk.

    `sb` ist der durable Stand (user_profiles-Row: profile-Dict),
    `disk` der Container-Payload (kann per `wipe_disk()` verschwinden —
    das ist die Redeploy-Simulation).
    """

    def __init__(self, sb=None, disk=None):
        self.sb = dict(sb) if sb else None
        self.disk = dict(disk) if disk else None
        self.sb_writes = []
        self.disk_writes = []

    # ── Patch-Targets ────────────────────────────────────────────────
    def load_sb(self, token):
        return dict(self.sb) if self.sb is not None else None

    def save_sb(self, token, profile):
        self.sb = dict(profile)
        self.sb_writes.append(dict(profile))
        return True

    def load_disk(self, token):
        if self.disk is None:
            return {'token': token, 'profile': {}}
        return json.loads(json.dumps(self.disk))

    def write_disk(self, path, payload):
        self.disk = json.loads(json.dumps(payload))
        self.disk_writes.append(self.disk)

    def wipe_disk(self):
        """Redeploy: der Container-Layer ist weg."""
        self.disk = None

    def patches(self):
        return (
            patch.object(A, '_profile_load_from_supabase', side_effect=self.load_sb),
            patch.object(A, '_profile_save_to_supabase', side_effect=self.save_sb),
            patch.object(A, '_profile_load_from_disk', side_effect=self.load_disk),
            patch.object(A, '_atomic_write_json', side_effect=self.write_disk),
            patch.object(A, '_user_profile_path',
                         side_effect=lambda t: '/tmp/_crewac_test_profile.json'),
            patch.object(A, 'SB_AVAILABLE', True),
            patch.object(A, '_validate_token', side_effect=_valid_token),
        )


def _run(store, fn):
    """Führt `fn()` unter allen Store-Patches aus."""
    ps = store.patches()
    for p in ps:
        p.start()
    try:
        return fn()
    finally:
        for p in reversed(ps):
            p.stop()


def _post(datum='2026-08-08', body=None, token=TOK, bearer=None):
    c = A.app.test_client()
    return c.post(f'/api/user/crew-aircraft/{token}/{datum}',
                  json=body if body is not None else {},
                  headers={'Authorization': f'Bearer {bearer or token}'})


def _get(token=TOK, bearer=None):
    c = A.app.test_client()
    return c.get(f'/api/user/crew-aircraft/{token}',
                 headers={'Authorization': f'Bearer {bearer or token}'})


# ════════════════════════════════════════════════════════════════════
# Schreiben landet in der DB
# ════════════════════════════════════════════════════════════════════

def test_post_writes_into_supabase_profile_not_only_disk():
    """Der Kern des Befunds: nach dem POST steht der Eintrag im durablen
    Profil (→ metadata-jsonb), nicht nur im Container-File."""
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK,
                                                    'profile': {'name': 'Miguel'}})
    r = _run(store, lambda: _post(body={
        'aircraft_reg': 'd-aixb', 'aircraft_type': 'A320',
        'crew': [{'name': 'Paula', 'function': 'PU'}],
    }))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert store.sb_writes, 'kein Supabase-Write — Fix greift nicht'
    saved = store.sb_writes[-1]['crew_aircraft']['2026-08-08']
    assert saved['aircraft_reg'] == 'D-AIXB'
    assert saved['crew'] == [{'name': 'Paula', 'function': 'PU'}]
    # Disk bleibt Spiegel (Legacy-Top-Level-Key für alte Reader).
    assert store.disk['crew_aircraft']['2026-08-08']['aircraft_reg'] == 'D-AIXB'
    assert store.disk['profile']['crew_aircraft']['2026-08-08']['aircraft_reg'] == 'D-AIXB'


def test_get_finds_what_post_wrote():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        _post(body={'notes': 'Catering vergessen'})
        return _get()

    r = _run(store, _flow)
    assert r.status_code == 200
    data = r.get_json()['data']
    assert data['2026-08-08']['notes'] == 'Catering vergessen'


def test_survives_redeploy_when_container_file_is_gone():
    """Redeploy-Simulation: Container-Disk weg, SB hat den Stand → der
    Eintrag ist weiterhin da (vorher: komplett verloren)."""
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        _post(body={'aircraft_reg': 'D-AIXB',
                    'crew': [{'name': 'Florian', 'function': 'FB'}]})
        store.wipe_disk()          # ← Deploy
        return _get()

    r = _run(store, _flow)
    data = r.get_json()['data']
    assert data['2026-08-08']['aircraft_reg'] == 'D-AIXB'
    assert data['2026-08-08']['crew'][0]['name'] == 'Florian'


def test_persist_failure_reports_500_instead_of_silent_ok():
    """Vorher wurde der Save-Rückgabewert ignoriert → 'ok': true trotz
    verlorener Daten."""
    store = _FakeStore(sb={'name': 'X'}, disk={'token': TOK, 'profile': {}})
    with patch.object(A, '_crew_aircraft_save', return_value=False):
        r = _run(store, lambda: _post(body={'notes': 'x'}))
    assert r.status_code == 500
    assert r.get_json()['error'] == 'persist_failed'


# ════════════════════════════════════════════════════════════════════
# Altdaten-Rettung (Warm-Migration)
# ════════════════════════════════════════════════════════════════════

def test_warm_migrates_legacy_container_entries_into_supabase():
    """Auf dem laufenden Container liegen noch Alt-Einträge im Top-Level-Key.
    Der erste Zugriff hebt sie in die DB — statt sie beim nächsten Deploy zu
    verlieren."""
    legacy = {'2026-07-01': {'aircraft_reg': 'D-AIAA',
                             'crew': [{'name': 'Janine', 'function': 'CM'}]}}
    store = _FakeStore(sb={'name': 'Miguel'},
                       disk={'token': TOK, 'profile': {'name': 'Miguel'},
                             'crew_aircraft': legacy})
    got = _run(store, lambda: A._crew_aircraft_load(TOK))
    assert got == legacy
    assert store.sb_writes, 'Altdaten wurden NICHT in die DB gehoben'
    assert store.sb_writes[-1]['crew_aircraft'] == legacy


def test_warm_migration_is_idempotent():
    """Zweiter Read schreibt nicht nochmal (merged == durable)."""
    legacy = {'2026-07-01': {'aircraft_reg': 'D-AIAA'}}
    store = _FakeStore(sb={'name': 'Miguel'},
                       disk={'token': TOK, 'profile': {'name': 'Miguel'},
                             'crew_aircraft': legacy})

    def _flow():
        A._crew_aircraft_load(TOK)
        n = len(store.sb_writes)
        A._crew_aircraft_load(TOK)
        return n, len(store.sb_writes)

    first, second = _run(store, _flow)
    assert first == 1
    assert second == 1, 'Warm-Migration lief ein zweites Mal'


def test_merge_is_newest_wins_and_legacy_only_fills_gaps():
    durable = {'2026-07-01': {'notes': 'DB', 'updated_at': '2026-08-01T00:00:00Z'},
               '2026-07-02': {'notes': 'DB-ohne-Stempel'}}
    legacy = {'2026-07-01': {'notes': 'ALT'},
              '2026-07-02': {'notes': 'ALT'},
              '2026-07-03': {'notes': 'NUR-ALT'}}
    merged = A._crew_aircraft_merge(durable, legacy)
    assert merged['2026-07-01']['notes'] == 'DB'
    assert merged['2026-07-02']['notes'] == 'DB-ohne-Stempel'
    assert merged['2026-07-03']['notes'] == 'NUR-ALT'


# ════════════════════════════════════════════════════════════════════
# Owner-only (PII: Crew-Namen sind Angaben über Dritte)
# ════════════════════════════════════════════════════════════════════

def test_get_route_is_registered_as_pii_prefix():
    assert A._bug004_get_route_needs_auth(f'/api/user/crew-aircraft/{TOK}') is True


def test_foreign_bearer_cannot_read():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _get(token=TOK, bearer=OTHER))
    assert r.status_code == 401
    assert r.get_json()['error'] == 'token_binding_mismatch'


def test_foreign_bearer_cannot_write():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _post(token=TOK, bearer=OTHER, body={'notes': 'leak'}))
    assert r.status_code == 401
    assert not store.sb_writes


def test_missing_bearer_is_rejected():
    """Prod-Modus (AEROX_REQUIRE_TOKEN_BINDING enforced — die Test-conftest
    setzt den Emergency-Opt-out '0')."""
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        with patch.object(A, '_BUG004_REQUIRE_TOKEN_BINDING', True):
            return A.app.test_client().get(f'/api/user/crew-aircraft/{TOK}')

    r = _run(store, _flow)
    assert r.status_code == 401
    assert r.get_json()['error'] == 'token_binding_required'


def test_crew_names_are_not_in_the_public_profile_projection():
    """Kein Freund/keine Familie bekommt den Store über den Profil-GET."""
    assert 'crew_aircraft' not in A._PUBLIC_PROFILE_FIELDS
    assert 'crew_aircraft' not in A._PROFILE_BULK_META_KEYS
    store = _FakeStore(sb={'name': 'Miguel',
                           'crew_aircraft': {'2026-08-08': {
                               'crew': [{'name': 'Paula', 'function': 'PU'}]}}},
                       disk={'token': TOK, 'profile': {}})
    pub = _run(store, lambda: A._public_profile_projection(TOK))
    assert 'crew_aircraft' not in pub['profile']


# ════════════════════════════════════════════════════════════════════
# Eingabe-Hygiene
# ════════════════════════════════════════════════════════════════════

def test_personalnummern_are_discarded():
    """Projekt-Regel: PKs fallen beim Parsen weg — Name + Funktion reichen."""
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _post(body={'crew': [
        {'name': 'Paula', 'function': 'PU', 'pk': '12345678',
         'lh_pk_number': '87654321', 'personalnummer': '111', 'staff_id': 'X'},
    ]}))
    assert r.status_code == 200
    saved = store.sb_writes[-1]['crew_aircraft']['2026-08-08']['crew'][0]
    assert saved == {'name': 'Paula', 'function': 'PU'}
    blob = json.dumps(store.sb_writes[-1])
    for needle in ('12345678', '87654321', 'personalnummer', 'staff_id'):
        assert needle not in blob


def test_invalid_datum_is_rejected():
    store = _FakeStore(sb={'name': 'X'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _post(datum='irgendwas', body={'notes': 'x'}))
    assert r.status_code == 400
    assert r.get_json()['error'] == 'invalid_datum'
    assert not store.sb_writes


def test_malformed_crew_entries_do_not_500():
    store = _FakeStore(sb={'name': 'X'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _post(body={'crew': ['nur-ein-string', None,
                                                 {'name': 'Ok', 'function': 'FB'}]}))
    assert r.status_code == 200
    assert store.sb_writes[-1]['crew_aircraft']['2026-08-08']['crew'] == [
        {'name': 'Ok', 'function': 'FB'}]


def test_notes_are_html_stripped_and_capped():
    store = _FakeStore(sb={'name': 'X'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _post(body={'notes': '<b>x</b>' + 'y' * 900}))
    assert r.status_code == 200
    note = store.sb_writes[-1]['crew_aircraft']['2026-08-08']['notes']
    assert '<b>' not in note
    assert len(note) <= A._FLIGHT_NOTE_MAX_CHARS


def test_store_cap_rejects_instead_of_dropping_days():
    """Kein stiller Datenverlust: über dem Dach kommt ein ehrliches 413."""
    store = _FakeStore(sb={'name': 'X'}, disk={'token': TOK, 'profile': {}})
    with patch.object(A, '_CREW_AIRCRAFT_MAX_BYTES', 50):
        r = _run(store, lambda: _post(body={'notes': 'z' * 200}))
    assert r.status_code == 413
    assert r.get_json()['error'] == 'store_full'
    assert not store.sb_writes
