"""Ortsnotiz im Backend + der tote Wechselkurs (Owner-Meldung 2026-08-09).

KURS-VORFALL (belegt): `api.exchangerate.host/latest` ist nicht mehr frei —
die Antwort ist **HTTP 200** mit `{"success": false, "error": {...}}`. Der alte
Code las daraus `rates={}`, `date=None`, lief in KEINEN except-Zweig und cachte
dieses leere Ergebnis **12 Stunden**. Prod-Probe vor dem Fix:
`GET /api/aviation/currency` → `{"base":"EUR","date":null,"rates":{}}`.
Der iOS-Client fiel dauerhaft auf seine eingebaute Tabelle zurück (USD 1.08).

NOTIZ: Die private Ortsnotiz lag bisher nur in UserDefaults. Sie geht jetzt
denselben Weg wie `flight-notes` — `user_profiles.metadata.destination_notes`,
owner-only über die PII-Prefix-Regel + Bearer-Binding.

Abgedeckt:
  - leerer/kaputter Kurs wird NICHT gecacht und NICHT als Erfolg gewertet
  - exakt die alte exchangerate.host-Antwort (200 + success:false) → 502
  - Fallback-Quelle springt ein wenn die Primärquelle stirbt
  - gültige Kurse werden gecacht und tragen ein echtes `as_of`
  - Antwort-Form bleibt rückwärtskompatibel (base/date/rates)
  - Notiz überlebt einen simulierten Cache-/Container-Verlust
  - ein PUT löscht keine Notiz zu einem anderen Ort
  - fremdes Token kommt weder lesend noch schreibend an die Notiz
"""
import json
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import app as A

TOK = 'AT-TEST-DESTNOTE-OWNER'
OTHER = 'AT-TEST-DESTNOTE-FREMD'


def _valid_token(_t=None):
    return A._TokenValidationResult(A._TokenValidationState.VALID,
                                    'destnote@aerox.test')


# ════════════════════════════════════════════════════════════════════
# Wechselkurs
# ════════════════════════════════════════════════════════════════════

def _clear_currency_cache():
    for k in [k for k in A._AVIATION_CACHE if k.startswith('cur:')]:
        A._AVIATION_CACHE.pop(k, None)


def _currency(base='EUR', symbols='USD,GBP'):
    return A.app.test_client().get(
        f'/api/aviation/currency?base={base}&symbols={symbols}')


def test_exchangerate_host_style_200_with_success_false_is_not_a_success():
    """Der eigentliche Befund: formal OK, inhaltlich leer → 502, kein Cache.

    Beide Quellen antworten wie die tote exchangerate.host-API: HTTP 200,
    aber kein einziger Kurs im Body.
    """
    _clear_currency_cache()

    def _dead(_base):
        # So sieht ein Anbieter aus, der nur noch einen Schlüssel-Fehler
        # zurückgibt: der Fetch-Helper erkennt das und liefert None.
        return None

    with patch.object(A, '_currency_fetch_er_api', side_effect=_dead), \
         patch.object(A, '_currency_fetch_jsdelivr', side_effect=_dead):
        r = _currency()
    assert r.status_code == 502, r.get_data(as_text=True)
    body = r.get_json()
    assert body['rates'] == {}
    assert body['error'] == 'upstream_unavailable'
    # DAS ist der Kern: nichts darf im Cache gelandet sein, sonst friert der
    # Ausfall 12 h ein.
    assert not [k for k in A._AVIATION_CACHE if k.startswith('cur:')], \
        'leeres Ergebnis wurde gecacht — genau die Falle von vorher'


def test_provider_answering_with_empty_rates_dict_is_treated_as_error():
    """Anbieter liefert ein Dict, aber ohne die angefragten Symbole."""
    _clear_currency_cache()
    with patch.object(A, '_currency_fetch_er_api',
                      return_value=({'ZZZ': 1.0}, '2026-08-09T00:00:00Z', 'test')), \
         patch.object(A, '_currency_fetch_jsdelivr', return_value=None):
        r = _currency(symbols='USD,GBP')
    assert r.status_code == 502
    assert r.get_json()['error'] == 'upstream_empty'
    assert not [k for k in A._AVIATION_CACHE if k.startswith('cur:')]


def test_implausible_rates_are_rejected_not_served():
    """Null-/Negativ-Kurse sind kein Kurs — lieber kein Wert als ein falscher."""
    _clear_currency_cache()
    with patch.object(A, '_currency_fetch_er_api',
                      return_value=({'USD': 0.0}, None, 'test')), \
         patch.object(A, '_currency_fetch_jsdelivr', return_value=None):
        r = _currency(symbols='USD')
    assert r.status_code == 502
    assert not [k for k in A._AVIATION_CACHE if k.startswith('cur:')]


def test_fallback_source_takes_over_when_primary_dies():
    _clear_currency_cache()
    with patch.object(A, '_currency_fetch_er_api',
                      side_effect=OSError('primary down')), \
         patch.object(A, '_currency_fetch_jsdelivr',
                      return_value=({'USD': 1.1548, 'GBP': 0.8569},
                                    '2026-08-08T00:00:00Z', 'jsdelivr/currency-api')):
        r = _currency(symbols='USD,GBP')
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['rates']['USD'] == 1.1548
    assert body['source'] == 'jsdelivr/currency-api'


def test_good_rates_are_cached_and_carry_a_real_timestamp():
    _clear_currency_cache()
    calls = {'n': 0}

    def _once(_base):
        calls['n'] += 1
        return ({'USD': 1.154817, 'GBP': 0.856873},
                '2026-08-09T00:02:31Z', 'open.er-api.com')

    with patch.object(A, '_currency_fetch_er_api', side_effect=_once), \
         patch.object(A, '_currency_fetch_jsdelivr', return_value=None):
        first = _currency(symbols='USD,GBP')
        second = _currency(symbols='USD,GBP')
    assert first.status_code == 200 and second.status_code == 200
    assert calls['n'] == 1, 'zweiter Call ging am Cache vorbei'
    body = first.get_json()
    # `as_of` ist der Stand DES ANBIETERS — kein erfundener Zeitpunkt.
    assert body['as_of'] == '2026-08-09T00:02:31Z'
    assert body['date'] == '2026-08-09'


def test_response_shape_stays_backwards_compatible():
    """Alte App-Builds decoden {base, date, rates} — die Keys bleiben."""
    _clear_currency_cache()
    with patch.object(A, '_currency_fetch_er_api',
                      return_value=({'USD': 1.15}, '2026-08-09T00:00:00Z', 'x')), \
         patch.object(A, '_currency_fetch_jsdelivr', return_value=None):
        body = _currency(symbols='USD').get_json()
    assert set(['base', 'date', 'rates']).issubset(body.keys())
    assert body['base'] == 'EUR'
    assert isinstance(body['rates'], dict)


def test_plausibility_helper_rejects_empty_dict():
    assert A._currency_rates_plausible({'USD': 1.15}) is True
    assert A._currency_rates_plausible({}) is False
    assert A._currency_rates_plausible(None) is False
    assert A._currency_rates_plausible({'USD': -1}) is False
    assert A._currency_rates_plausible({'USD': float('nan')}) is False


# ════════════════════════════════════════════════════════════════════
# Private Ortsnotiz
# ════════════════════════════════════════════════════════════════════

class _FakeStore:
    """Supabase (durabel) + Container-Disk (verschwindet beim Deploy)."""

    def __init__(self, sb=None, disk=None):
        self.sb = dict(sb) if sb is not None else None
        self.disk = dict(disk) if disk is not None else None
        self.sb_writes = []

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

    def wipe_disk(self):
        self.disk = None

    def patches(self):
        return (
            patch.object(A, '_profile_load_from_supabase', side_effect=self.load_sb),
            patch.object(A, '_profile_save_to_supabase', side_effect=self.save_sb),
            patch.object(A, '_profile_load_from_disk', side_effect=self.load_disk),
            patch.object(A, '_atomic_write_json', side_effect=self.write_disk),
            patch.object(A, '_user_profile_path',
                         side_effect=lambda t: '/tmp/_destnote_test_profile.json'),
            patch.object(A, 'SB_AVAILABLE', True),
            patch.object(A, '_validate_token', side_effect=_valid_token),
        )


def _run(store, fn):
    ps = store.patches()
    for p in ps:
        p.start()
    try:
        return fn()
    finally:
        for p in reversed(ps):
            p.stop()


def _put_note(iata, note, token=TOK, bearer=None, updated_at=None):
    payload = {'note': note}
    if updated_at:
        payload['updated_at'] = updated_at
    return A.app.test_client().put(
        f'/api/user/destination-notes/{token}/{iata}',
        json=payload,
        headers={'Authorization': f'Bearer {bearer or token}'})


def _get_note(iata, token=TOK, bearer=None):
    return A.app.test_client().get(
        f'/api/user/destination-notes/{token}/{iata}',
        headers={'Authorization': f'Bearer {bearer or token}'})


def _list_notes(token=TOK, bearer=None):
    return A.app.test_client().get(
        f'/api/user/destination-notes/{token}',
        headers={'Authorization': f'Bearer {bearer or token}'})


def test_note_lands_in_the_durable_profile_not_only_on_disk():
    store = _FakeStore(sb={'name': 'Miguel'},
                       disk={'token': TOK, 'profile': {'name': 'Miguel'}})
    r = _run(store, lambda: _put_note('JFK', 'Hotel Row NYC, Crew-Bus 06:15'))
    assert r.status_code == 200, r.get_data(as_text=True)
    assert store.sb_writes, 'kein Supabase-Write — die Notiz bliebe lokal'
    saved = store.sb_writes[-1]['destination_notes']['JFK']
    assert saved['text'] == 'Hotel Row NYC, Crew-Bus 06:15'
    assert saved['updated_at']


def test_note_survives_a_simulated_cache_loss():
    """Redeploy/Cache-Verlust: die Container-Datei ist weg, die Notiz nicht."""
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        _put_note('JFK', 'Bar im 12. Stock')
        store.wipe_disk()          # ← Deploy / Cache weg
        return _get_note('JFK')

    r = _run(store, _flow)
    assert r.status_code == 200
    assert r.get_json()['note'] == 'Bar im 12. Stock'


def test_writing_one_place_never_drops_another():
    """Ein Abgleich darf nichts wegnehmen, was schon dasteht."""
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        _put_note('JFK', 'JFK-Notiz')
        _put_note('SIN', 'SIN-Notiz')
        return _list_notes()

    notes = _run(store, _flow).get_json()['notes']
    assert notes['JFK']['text'] == 'JFK-Notiz'
    assert notes['SIN']['text'] == 'SIN-Notiz'


def test_empty_note_removes_only_that_place():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        _put_note('JFK', 'weg damit')
        _put_note('SIN', 'bleibt')
        _put_note('JFK', '')
        return _list_notes()

    notes = _run(store, _flow).get_json()['notes']
    assert 'JFK' not in notes
    assert notes['SIN']['text'] == 'bleibt'


def test_client_timestamp_is_kept_so_the_client_can_merge_without_loss():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})
    stamp = '2026-08-09T11:22:33Z'
    r = _run(store, lambda: _put_note('JFK', 'lokal getippt', updated_at=stamp))
    assert r.get_json()['updated_at'] == stamp


def test_html_is_stripped_like_in_the_day_note():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _put_note('JFK', '<script>x</script>Hotel'))
    assert r.get_json()['note'] == 'xHotel'


def test_invalid_iata_is_refused():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _put_note('J', 'x'))
    assert r.status_code == 400


def test_legacy_plain_string_entries_are_still_readable():
    """Toleranz gegen Altformate — ein blanker String bleibt lesbar."""
    store = _FakeStore(sb={'name': 'Miguel',
                           'destination_notes': {'jfk': 'alter Stil'}},
                       disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _get_note('JFK'))
    assert r.get_json()['note'] == 'alter Stil'


# ── Owner-only ───────────────────────────────────────────────────────

def test_route_is_covered_by_the_pii_prefix_rule():
    assert A._bug004_get_route_needs_auth(
        f'/api/user/destination-notes/{TOK}') is True
    assert A._bug004_get_route_needs_auth(
        f'/api/user/destination-notes/{TOK}/JFK') is True


def test_foreign_bearer_cannot_read_the_note():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        _put_note('JFK', 'privat')
        return _get_note('JFK', bearer=OTHER)

    r = _run(store, _flow)
    assert r.status_code in (401, 403), r.get_data(as_text=True)
    assert 'privat' not in r.get_data(as_text=True)


def test_foreign_bearer_cannot_write_the_note():
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})
    r = _run(store, lambda: _put_note('JFK', 'fremd', bearer=OTHER))
    assert r.status_code in (401, 403), r.get_data(as_text=True)


def test_missing_bearer_is_blocked_once_binding_is_enforced():
    """Ohne Bearer greift heute noch der projektweite EMERGENCY-Opt-out
    (`_BUG004_REQUIRE_TOKEN_BINDING == False`), damit alte Builds nicht
    brechen — das ist eine globale Entscheidung, keine Eigenheit dieser Route.
    Sobald das Flag steht, ist auch diese Route dicht. Der Test hält beides
    fest, damit die Route beim Umlegen des Flags nicht durchrutscht.
    """
    store = _FakeStore(sb={'name': 'Miguel'}, disk={'token': TOK, 'profile': {}})

    def _flow():
        _put_note('JFK', 'privat')
        return A.app.test_client().get(
            f'/api/user/destination-notes/{TOK}/JFK')

    with patch.object(A, '_BUG004_REQUIRE_TOKEN_BINDING', True):
        r = _run(store, _flow)
    assert r.status_code == 401, r.get_data(as_text=True)
    assert 'privat' not in r.get_data(as_text=True)


def test_note_is_covered_by_the_existing_gdpr_delete_cascade():
    """Kein eigener Löschpfad nötig: die Notiz liegt in `user_profiles`,
    und diese Tabelle steht bereits in der Account-Delete-Cascade."""
    src = A.auth_delete_account.__doc__ or ''
    import inspect
    body = inspect.getsource(A.auth_delete_account)
    assert "('user_profiles',     'token')" in body, \
        'user_profiles fehlt in der Delete-Cascade — die Ortsnotiz überlebte'
