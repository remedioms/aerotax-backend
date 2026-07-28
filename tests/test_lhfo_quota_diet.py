"""FlightOps-Quota-Diät (2026-07-28) — ~30 → ~10 Calls/User/Tag.

Drei Bausteine, alle hier abgesichert:
  1. TAGES-Deckel im Key-Gate (der Key hat neben 1.000/h auch 6.000/Tag).
  2. LAZY ROTATION: der Ein-Refresher rotiert nur noch bei Bedarf (Demand)
     oder Keepalive (>20 h) statt ~32×/Tag pro Grant.
  3. ADAPTIVE SYNC-KADENZ im 2-h-Cron: 3,5 h bei Dienst in Sicht, sonst 11,5 h.

Die Grant-Burn-Schutzarchitektur ist bewusst NICHT Gegenstand dieser Diät:
rotiert wird weiterhin ausschließlich im Refresher-Thread (Choke-Point-Gate
in _refresh), fail-closed bleibt fail-closed.
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

import app  # noqa: F401  (Blueprint-Registrierung)
from blueprints import lh_flightops as fo


def _with_access():
    return patch.object(fo, '_valid_access', return_value='AT-FAKE-ACCESS')


# ── 1. TAGES-DECKEL ─────────────────────────────────────────────────────────
import pytest


@pytest.fixture(autouse=True)
def _clean_fo_state():
    """Modul-globalen Quota-Diät-State vor UND nach jedem Test räumen —
    _fo_last_sync/_refresher_demand leaken sonst in fremde Tests
    (Suite-Ordnungs-Rot: test_refresh_all_work_counts_and_releases_lock sah
    deferred-Tokens aus diesen Tests)."""
    fo._fo_last_sync.clear()
    fo._refresher_demand.clear()
    yield
    fo._fo_last_sync.clear()
    fo._refresher_demand.clear()


def test_day_gate_blocks_background_at_day_ceiling():
    """Hintergrund stoppt beim Tages-Deckel — obwohl die Stunde entspannt ist."""
    booked = []
    with _with_access(), \
         patch.object(fo, '_rot_hour_used', return_value=0), \
         patch.object(fo, '_lhfo_day_used',
                      return_value=fo._LHFO_DAY_BACKGROUND_CEILING), \
         patch.object(fo, '_flightops_budget_inc',
                      side_effect=lambda p: booked.append(p)):
        assert fo._api_get('tok', '/COMMON_DUTY_EVENTS') is None
    assert booked == []          # gar nicht erst gebucht/gesendet


def test_day_gate_leaves_interactive_headroom():
    """Interaktive Flows (Connect, „Jetzt aktualisieren", Re-Login-Heilung)
    dürfen in den reservierten Tages-Headroom."""
    sent = []
    with _with_access(), \
         patch.object(fo, '_rot_hour_used', return_value=0), \
         patch.object(fo, '_lhfo_day_used',
                      return_value=fo._LHFO_DAY_BACKGROUND_CEILING), \
         patch.object(fo, '_flightops_budget_inc',
                      side_effect=lambda p: sent.append(p)), \
         patch.object(fo.urllib.request, 'urlopen',
                      side_effect=RuntimeError('kein echter Call im Test')):
        fo._api_get('tok', '/COMMON_DUTY_EVENTS', interactive=True)
    assert sent == ['/COMMON_DUTY_EVENTS']


def test_day_gate_blocks_interactive_at_hard_day_ceiling():
    sent = []
    with _with_access(), \
         patch.object(fo, '_rot_hour_used', return_value=0), \
         patch.object(fo, '_lhfo_day_used',
                      return_value=fo._LHFO_DAY_INTERACTIVE_CEILING), \
         patch.object(fo, '_flightops_budget_inc',
                      side_effect=lambda p: sent.append(p)):
        assert fo._api_get('tok', '/COMMON_DUTY_EVENTS',
                           interactive=True) is None
    assert sent == []
    # Die Deckel müssen unter dem LH-Tageslimit (6.000) bleiben — die 403s des
    # Gateways zählen selbst aufs Kontingent, wir stoppen VORHER.
    assert (fo._LHFO_DAY_BACKGROUND_CEILING
            < fo._LHFO_DAY_INTERACTIVE_CEILING < 6000)


def test_budget_inc_books_hour_and_day_key():
    """Jeder Call bucht Stunden- UND Tages-Schlüssel; der Tages-Key ist per
    _budget_key_used('lhfoD:<YYYYMMDD>') lesbar (kein Stunden-Suffix)."""
    hour, day = [], []
    with patch('blueprints.lh_open_api.budget_inc',
               side_effect=lambda pre, svc=None, units=1: hour.append((pre, svc))), \
         patch('blueprints.lh_open_api.budget_inc_key',
               side_effect=lambda k, units=1: day.append(k)):
        fo._flightops_budget_inc('/COMMON_DUTY_EVENTS')
    assert hour == [('lhfo', 'COMMON_DUTY_EVENTS')]
    assert len(day) == 1
    assert day[0].startswith('lhfoD:') and len(day[0]) == len('lhfoD:20260728')


# ── 2. LAZY ROTATION ────────────────────────────────────────────────────────
def _grant(now, age_h=0.2):
    """Grant, dessen letzte Rotation `age_h` Stunden her ist (abgeleitet über
    expires_at − AT-Lebensdauer, s. _refresher_due)."""
    return {'refresh': 'R',
            'expires_at': now - age_h * 3600 + fo._REFRESHER_AT_LIFETIME_S}


def test_lazy_no_rotation_without_demand_or_keepalive():
    """DER Kostenblock: ein gerade rotierter Grant, dessen AT in <15 min
    abläuft, ist OHNE Bedarf NICHT mehr fällig (vorher: ~32 Rotationen/Tag)."""
    now = 1000000.0
    scan = [('AT-A', {'refresh': 'R', 'expires_at': now + 600})]
    assert fo._refresher_due(scan, now=now, demand=set()) == []


def test_lazy_demand_makes_due():
    now = 1000000.0
    scan = [('AT-A', {'refresh': 'R', 'expires_at': now + 600})]
    assert fo._refresher_due(scan, now=now, demand={'AT-A'}) == ['AT-A']


def test_lazy_keepalive_after_20h():
    """RT-Hygiene: jeder gesunde Grant rotiert mindestens ~1×/20 h, auch ohne
    jeden Bedarf (der Refresh-Token darf nicht idle sterben)."""
    now = 1000000.0
    scan = [('AT-YOUNG', _grant(now, age_h=19.0)),
            ('AT-OLD', _grant(now, age_h=21.0))]
    assert fo._refresher_due(scan, now=now, demand=set()) == ['AT-OLD']


def test_lazy_never_touches_dead_or_rtless_grants():
    """needs_relogin / kein RT: nie fällig — auch nicht mit Demand und auch
    nicht über Keepalive."""
    now = 1000000.0
    scan = [('AT-DEAD', {'refresh': 'R', 'needs_relogin': True,
                         'expires_at': now - 100000}),
            ('AT-NORT', {'expires_at': now - 100000})]
    assert fo._refresher_due(scan, now=now,
                             demand={'AT-DEAD', 'AT-NORT'}) == []
    assert fo._refresher_due(scan, now=now, demand=set()) == []


# ── 2c. CROSS-CONTAINER-POKE ────────────────────────────────────────────────
def test_rotate_poke_requires_secret(monkeypatch):
    monkeypatch.setenv('ADSB_POLL_SECRET', 'topsecret')
    fo._refresher_demand.discard('AT-POKE')
    c = app.app.test_client()
    r = c.post('/api/internal/flightops/rotate-poke', json={'token': 'AT-POKE'})
    assert r.status_code == 403
    assert 'AT-POKE' not in fo._refresher_demand


def test_rotate_poke_queues_demand(monkeypatch):
    monkeypatch.setenv('ADSB_POLL_SECRET', 'topsecret')
    fo._refresher_demand.discard('AT-POKE2')
    try:
        c = app.app.test_client()
        d = c.post('/api/internal/flightops/rotate-poke',
                   json={'token': 'AT-POKE2'},
                   headers={'X-Poll-Secret': 'topsecret'}).get_json()
        assert d == {'ok': True, 'queued': True}
        assert 'AT-POKE2' in fo._refresher_demand
    finally:
        fo._refresher_demand.discard('AT-POKE2')


def test_import_pending_pokes_demand_and_keeps_response_shape(monkeypatch):
    """iOS-Contract: die 503-Antwort bleibt Zeichen für Zeichen dieselbe —
    NEU ist nur der Demand-Poke (lokal + best-effort über das interne Netz)."""
    poked = []
    fo._refresher_demand.discard('AT-PEND')
    monkeypatch.setattr(fo, 'flightops_configured', lambda: True)
    monkeypatch.setattr(fo, '_access_state', lambda t: ('pending', None))
    monkeypatch.setattr(fo, '_rotate_poke_remote',
                        lambda t: poked.append(t) or True)
    monkeypatch.setattr(app, '_validate_token', lambda *a, **k: True)
    try:
        r = app.app.test_client().post(
            '/api/lh/flightops/import/AT-PEND', json={},
            headers={'Authorization': 'Bearer AT-PEND'})
        assert r.status_code == 503
        assert r.get_json() == {'ok': False, 'error': 'token_refresh_pending'}
        assert poked == ['AT-PEND']
        assert 'AT-PEND' in fo._refresher_demand
    finally:
        fo._refresher_demand.discard('AT-PEND')


def test_rotate_poke_remote_never_raises(monkeypatch):
    """Best-effort heißt best-effort: kein Poll-Container erreichbar ⇒ der
    User-Request läuft trotzdem normal weiter."""
    monkeypatch.setattr(fo.urllib.request, 'urlopen',
                        lambda *a, **k: (_ for _ in ()).throw(OSError('nope')))
    assert fo._rotate_poke_remote('AT-X') is False


# ── 3. ADAPTIVE SYNC-KADENZ ─────────────────────────────────────────────────
def _brief_flight(days_ahead, now):
    from datetime import datetime as _d, timedelta as _td, timezone as _tz
    day = (_d.fromtimestamp(now, _tz.utc) + _td(days=days_ahead)
           ).strftime('%Y-%m-%d')
    return {day: {'ical_summary': 'LH400 FRA-JFK',
                  'ical_sectors': [{'fno': 'LH400', 'from': 'FRA',
                                    'to': 'JFK'}]}}


def _patch_briefings(monkeypatch, fn):
    """`_fo_duty_within` liest über `import app as _app` — im Full-Run kann
    `sys.modules['app']` eine ANDERE Kopie sein als die zur Collection-Zeit
    importierte (test_calculation tauscht sie per Reimport-Trick aus, siehe
    tests/_clock_freeze.py). Deshalb BEIDE Kopien patchen, sonst ist der Test
    einzeln grün und im Full-Run rot."""
    import sys as _sys
    mods = [app]
    cur = _sys.modules.get('app')
    if cur is not None and all(cur is not m for m in mods):
        mods.append(cur)
    for m in mods:
        monkeypatch.setattr(m, '_ical_briefings_load', fn, raising=False)


def test_cadence_first_contact_always_syncs(monkeypatch):
    monkeypatch.setattr(fo, '_fo_last_sync', {})
    do, why = fo._fo_should_sync('AT-NEW', now=1000000.0)
    assert do is True and why == 'first'


def test_cadence_near_duty_uses_35h_rule(monkeypatch):
    """Dienst morgen ⇒ 3,5-h-Regel: nach 3 h noch nicht, nach 4 h ja."""
    now = 1000000.0
    _patch_briefings(monkeypatch, lambda t: _brief_flight(1, now))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-N': now - 3 * 3600})
    assert fo._fo_should_sync('AT-N', now=now) == (False, 'too_soon')
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-N': now - 4 * 3600})
    assert fo._fo_should_sync('AT-N', now=now) == (True, 'duty_near')


def test_cadence_far_duty_uses_115h_rule(monkeypatch):
    """Nächster Dienst erst in 10 Tagen ⇒ 11,5-h-Regel."""
    now = 1000000.0
    _patch_briefings(monkeypatch, lambda t: _brief_flight(10, now))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-F': now - 4 * 3600})
    assert fo._fo_should_sync('AT-F', now=now) == (False, 'no_duty_near')
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-F': now - 12 * 3600})
    assert fo._fo_should_sync('AT-F', now=now) == (True, 'far_due')


def test_cadence_unknown_duty_uses_115h_rule(monkeypatch):
    """Keine Briefings / nur freie Tage = „kein Dienst bekannt" ⇒ locker."""
    now = 1000000.0
    from datetime import datetime as _d, timezone as _tz
    today = _d.fromtimestamp(now, _tz.utc).strftime('%Y-%m-%d')
    _patch_briefings(monkeypatch,
                     lambda t: {today: {'ical_klass': 'frei',
                                        'ical_summary': 'OFF DAY'}})
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-U': now - 6 * 3600})
    assert fo._fo_should_sync('AT-U', now=now) == (False, 'no_duty_near')
    _patch_briefings(monkeypatch, lambda t: {})
    assert fo._fo_should_sync('AT-U', now=now) == (False, 'no_duty_near')


def test_cadence_standby_counts_as_duty(monkeypatch):
    """Standby ist Dienst (Anfahrt möglich) — enge Taktung."""
    now = 1000000.0
    from datetime import datetime as _d, timedelta as _td, timezone as _tz
    day = (_d.fromtimestamp(now, _tz.utc) + _td(days=1)).strftime('%Y-%m-%d')
    _patch_briefings(monkeypatch,
                     lambda t: {day: {'ical_klass': 'standby',
                                      'ical_summary': 'Standby (SB60)'}})
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-S': now - 5 * 3600})
    assert fo._fo_should_sync('AT-S', now=now) == (True, 'duty_near')


def test_refresh_all_defers_and_counts(monkeypatch):
    """Der 2-h-Lauf synct nur die fälligen Tokens und meldet deferred."""
    calls = []
    monkeypatch.setattr(fo, '_fo_should_sync',
                        lambda tok, now=None: ((True, 'first') if tok == 'AT-A'
                                               else (False, 'too_soon')))
    monkeypatch.setattr(fo, '_fo_demand_prephase', lambda toks: 0)
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_work(['AT-A', 'AT-B', 'AT-C'])
    assert calls == ['AT-A']
    st = fo._refresh_all_state['last']
    assert st['ok'] == 1 and st['deferred'] == 2 and st['users'] == 3


# ── 2b(ii). DEMAND-VORLAUF ──────────────────────────────────────────────────
def test_demand_prephase_pokes_and_import_follows_after_rotation(monkeypatch):
    """Pending-Token wird gepokt, der (gefakte) Refresher rotiert, danach
    importiert der Lauf ihn normal."""
    state = {'AT-P': 'pending'}
    poked = []

    def _fake_rotate(tok):
        poked.append(tok)
        state[tok] = 'ok'          # „der Refresher hat rotiert"
        return True

    monkeypatch.setattr(fo, '_access_state',
                        lambda tok: (state.get(tok, 'ok'),
                                     None if state.get(tok) == 'pending'
                                     else 'ACC'))
    monkeypatch.setattr(fo, '_rotate_poke_remote', _fake_rotate)
    monkeypatch.setattr(fo, '_fo_should_sync', lambda tok, now=None: (True, 'x'))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    calls = []
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    try:
        fo._refresh_all_work(['AT-P'])
    finally:
        fo._refresher_demand.discard('AT-P')
    assert poked == ['AT-P']
    assert calls == ['AT-P']
    assert fo._refresh_all_state['last']['ok'] == 1


def test_demand_prephase_skips_still_pending(monkeypatch):
    """Bleibt der Grant nach dem Vorlauf pending, wird er übersprungen —
    NIEMALS selbst rotiert (der Refresher ist der einzige Rotierer)."""
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('pending', None))
    monkeypatch.setattr(fo, '_rotate_poke_remote', lambda tok: True)
    monkeypatch.setattr(fo, '_fo_should_sync', lambda tok, now=None: (True, 'x'))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    calls = []
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    try:
        fo._refresh_all_work(['AT-STUCK'])
    finally:
        fo._refresher_demand.discard('AT-STUCK')
    assert calls == []
    st = fo._refresh_all_state['last']
    assert st['skipped'] == 1 and st['ok'] == 0


def test_demand_cap_ignores_overflow():
    """Deckel schützt gegen einen entarteten Poker — bestehende Demands
    bleiben stehen (Set wird NICHT geleert)."""
    saved = set(fo._refresher_demand)
    try:
        fo._refresher_demand.clear()
        for i in range(fo._REFRESHER_DEMAND_CAP):
            fo._refresher_demand.add('AT-%d' % i)
        assert fo._refresher_demand_add('AT-OVERFLOW') is False
        assert 'AT-OVERFLOW' not in fo._refresher_demand
        assert len(fo._refresher_demand) == fo._REFRESHER_DEMAND_CAP
    finally:
        fo._refresher_demand.clear()
        fo._refresher_demand.update(saved)
