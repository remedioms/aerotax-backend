"""FlightOps-Quota-Diät (2026-07-28) — ~30 → ~10 Calls/User/Tag.

Drei Bausteine, alle hier abgesichert:
  1. TAGES-Deckel im Key-Gate (der Key hat neben 1.000/h auch 6.000/Tag).
  2. LAZY ROTATION: der Ein-Refresher rotiert nur noch bei Bedarf (Demand)
     oder Keepalive (>18 h) statt ~32×/Tag pro Grant.
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


def test_lazy_keepalive_after_12h():
    """RT-Hygiene: jeder gesunde Grant rotiert mindestens ~1×/12 h, auch ohne
    jeden Bedarf (RT-Lebensdauer laut Gateway 14 h; 12 h + max. 1 h Backoff
    bleibt sicher darunter — Mashery-Enforcement ab September 2026)."""
    now = 1000000.0
    scan = [('AT-YOUNG', _grant(now, age_h=11.0)),
            ('AT-OLD', _grant(now, age_h=13.0))]
    assert fo._refresher_due(scan, now=now, demand=set()) == ['AT-OLD']
    # Invariante: Keepalive + maximaler Fehler-Backoff müssen unter der
    # RT-Lebensdauer von 14 h liegen, sonst können idle Grants sterben.
    assert fo._REFRESHER_KEEPALIVE_S + fo._ROT_BACKOFF_MAX_S < 14 * 3600


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


def _day_str(days_ahead, now):
    from datetime import datetime as _d, timedelta as _td, timezone as _tz
    return (_d.fromtimestamp(now, _tz.utc) + _td(days=days_ahead)
            ).strftime('%Y-%m-%d')


def _brief_days(now, duty_days=(), free_until=30, extra=None):
    """Roster-Fixture: freie Tage bis `free_until`, Dienst (Legs) an den
    `duty_days`-Offsets, optional weitere Tage via `extra`."""
    out = {}
    for i in range(0, free_until + 1):
        out[_day_str(i, now)] = {'ical_klass': 'frei',
                                 'ical_summary': 'OFF DAY'}
    for i in duty_days:
        out.update(_brief_flight(i, now))
    for k, v in (extra or {}).items():
        out[k] = v
    return out


def test_cadence_fast_duty_today_or_tomorrow(monkeypatch):
    """Dienst morgen ⇒ fast (3,5 h): nach 3 h noch nicht, nach 4 h ja."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    _patch_briefings(monkeypatch,
                     lambda t: _brief_days(now, duty_days=(1,)))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-N': now - 3 * 3600})
    assert fo._fo_should_sync('AT-N', now=now) == (False, 'skip_fast')
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-N': now - 4 * 3600})
    assert fo._fo_should_sync('AT-N', now=now) == (True, 'fast')


def test_cadence_mid_duty_in_2_to_7_days(monkeypatch):
    """Dienst in 3 Tagen ⇒ mid (11,5 h): nach 8 h nein, nach 12 h ja."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    _patch_briefings(monkeypatch,
                     lambda t: _brief_days(now, duty_days=(3,)))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-M': now - 8 * 3600})
    assert fo._fo_should_sync('AT-M', now=now) == (False, 'skip_mid')
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-M': now - 12 * 3600})
    assert fo._fo_should_sync('AT-M', now=now) == (True, 'mid')


def test_cadence_slow_no_duty_in_horizon(monkeypatch):
    """Nächster Dienst erst in 10 Tagen ⇒ slow (21,5 h, 1×/Tag)."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    _patch_briefings(monkeypatch,
                     lambda t: _brief_days(now, duty_days=(10,)))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-F': now - 12 * 3600})
    assert fo._fo_should_sync('AT-F', now=now) == (False, 'skip_slow')
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-F': now - 22 * 3600})
    assert fo._fo_should_sync('AT-F', now=now) == (True, 'slow')


def test_cadence_vacation_only_is_slow(monkeypatch):
    """Nur Urlaub/Frei im Horizont, Roster reicht weit ⇒ slow."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    _patch_briefings(
        monkeypatch,
        lambda t: {_day_str(i, now): {'ical_summary': 'URLAUB'}
                   for i in range(0, 25)})
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-V': now - 12 * 3600})
    assert fo._fo_should_sync('AT-V', now=now) == (False, 'skip_slow')


def test_cadence_empty_store_is_failsafe_fast(monkeypatch):
    """Neuverbindung/leerer Store und Loader-Fehler ⇒ fast — ein User ohne
    Briefings darf NIE ausgehungert werden (fail-safe: lieber ein Call zu
    viel als ein staler Dienstplan)."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    _patch_briefings(monkeypatch, lambda t: {})
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-E': now - 4 * 3600})
    assert fo._fo_should_sync('AT-E', now=now) == (True, 'fast')

    def _boom(t):
        raise RuntimeError('sb down')
    _patch_briefings(monkeypatch, _boom)
    assert fo._fo_should_sync('AT-E', now=now) == (True, 'fast')
    # Nur Müll-Keys ⇒ ebenfalls fail-safe fast
    _patch_briefings(monkeypatch, lambda t: {'kaputt': {'x': 1}})
    assert fo._fo_should_sync('AT-E', now=now) == (True, 'fast')


def test_cadence_standby_and_reserve_get_fastest_class(monkeypatch):
    """Standby/Reserve heute-morgen ohne Legs ⇒ fast_sb (1,9 h = jeder
    Cron-Lauf). RB zählt als Reserve; LISBOA (SB-Substring-Falle) NICHT."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    # Standby morgen: nach 2 h fällig (fast wäre erst bei 3,5 h)
    _patch_briefings(
        monkeypatch,
        lambda t: _brief_days(now, extra={
            _day_str(1, now): {'ical_klass': 'standby',
                               'ical_summary': 'Standby (SB60)'}}))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-S': now - 2 * 3600})
    assert fo._fo_should_sync('AT-S', now=now) == (True, 'fast_sb')
    # RB = Reserve (ganzes Wort) — gleiche Klasse
    _patch_briefings(
        monkeypatch,
        lambda t: _brief_days(now, extra={
            _day_str(0, now): {'ical_summary': 'RB'}}))
    assert fo._fo_should_sync('AT-S', now=now) == (True, 'fast_sb')
    # LISBOA trägt 'SB' als Substring, ist aber KEIN Standby — und ohne
    # Dienst-Evidenz bleibt der Tag frei ⇒ slow-Klasse, kein Sync nach 2 h
    _patch_briefings(
        monkeypatch,
        lambda t: _brief_days(now, extra={
            _day_str(0, now): {'ical_summary': 'HOTAC LISBOA'}}))
    assert fo._fo_should_sync('AT-S', now=now) == (False, 'skip_slow')
    # Standby MIT schon zugewiesenen Legs = normaler Dienst ⇒ fast, nicht
    # fast_sb (der Abruf ist passiert, der Tag ist ein Flugtag)
    d1 = _brief_flight(1, now)
    for _ev in d1.values():
        _ev['ical_klass'] = 'standby'
    _patch_briefings(monkeypatch,
                     lambda t: _brief_days(now, extra=d1))
    assert fo._fo_should_sync('AT-S', now=now) == (False, 'skip_fast')


def test_cadence_overnight_split_day_counts_as_duty(monkeypatch):
    """„(Tag 2/2)"-Zeilen (Übernacht-Split) tragen keine ical_sectors — der
    Flug steht nur in der Summary (DB-Beleg 31.07.: Owner-Flugtag
    'LH 455: SFO-FRA (Tag 2/2) · X', sectors leer, klass None). Das
    IATA-Paar in der Summary zählt als Dienst ⇒ fast, nicht slow."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    _patch_briefings(
        monkeypatch,
        lambda t: _brief_days(now, extra={
            _day_str(0, now): {
                'ical_summary': 'LH 455: SFO-FRA (Tag 2/2) · X'}}))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-O': now - 4 * 3600})
    assert fo._fo_should_sync('AT-O', now=now) == (True, 'fast')


def test_cadence_roster_end_bumps_to_mid(monkeypatch):
    """Roster-Abdeckung endet in ≤7 Tagen ⇒ mind. mid, auch ohne Dienst —
    die Monats-Veröffentlichung will niemand erst nach 22 h sehen."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    _patch_briefings(monkeypatch,
                     lambda t: _brief_days(now, free_until=5))
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-R': now - 12 * 3600})
    assert fo._fo_should_sync('AT-R', now=now) == (True, 'mid')
    # Abdeckung nur Vergangenheit (Roster komplett ausgelaufen) ⇒ ebenfalls mid
    _patch_briefings(
        monkeypatch,
        lambda t: {_day_str(-i, now): {'ical_summary': 'OFF DAY'}
                   for i in range(3, 10)})
    assert fo._fo_should_sync('AT-R', now=now) == (True, 'mid')


def test_cadence_reclassified_every_run_slow_to_fast(monkeypatch):
    """Der Wechsel langsam→schnell passiert im NÄCHSTEN Lauf von selbst:
    die Klasse wird pro Lauf aus dem GESPEICHERTEN Roster neu berechnet,
    nie gecacht. Gleicher Token, gleiche Uhr — nur der Store ändert sich."""
    now = 1000000.0
    monkeypatch.setattr(fo, '_fo_homebase', {})
    monkeypatch.setattr(fo, '_fo_last_sync', {'AT-W': now - 4 * 3600})
    _patch_briefings(monkeypatch,
                     lambda t: _brief_days(now, duty_days=(10,)))
    assert fo._fo_should_sync('AT-W', now=now) == (False, 'skip_slow')
    # Neuer Dienst morgen erschien im letzten Sync → Store trägt ihn jetzt
    _patch_briefings(monkeypatch,
                     lambda t: _brief_days(now, duty_days=(1, 10)))
    assert fo._fo_should_sync('AT-W', now=now) == (True, 'fast')


def test_cadence_today_anchors_on_homebase_timezone(monkeypatch):
    """ZEITZONEN-ZELLE (teuerste Fehlerklasse): 23:30 UTC ist in Berlin schon
    der Folgetag. Dienst am Berlin-„morgen" muss fast sein (UTC-Rechnung
    ergäbe fälschlich 2 Tage = mid). Und eine JFK-Homebase rechnet mit IHREM
    Kalender. Läuft bewusst unter fremd gestellter Prozess-Zeitzone."""
    import os
    import time as _time
    from datetime import datetime as _d, timezone as _tz
    old_tz = os.environ.get('TZ')
    os.environ['TZ'] = 'Pacific/Auckland'      # fremde Gerätezone
    _time.tzset()
    try:
        now = _d(2026, 8, 5, 23, 30, tzinfo=_tz.utc).timestamp()
        duty_day = '2026-08-07'                 # Berlin: morgen · UTC: +2 Tage
        briefs = {duty_day: {'ical_summary': 'LH400 FRA-JFK',
                             'ical_sectors': [{'fno': 'LH400'}]}}
        briefs.update({f'2026-08-{d:02d}': {'ical_summary': 'OFF DAY'}
                       for d in range(8, 31)})
        _patch_briefings(monkeypatch, lambda t: dict(briefs))
        monkeypatch.setattr(fo, '_fo_homebase', {'AT-TZ': 'FRA'})
        monkeypatch.setattr(fo, '_fo_last_sync', {'AT-TZ': now - 4 * 3600})
        assert fo._fo_should_sync('AT-TZ', now=now) == (True, 'fast')
        # Ohne Homebase-Eintrag: Fallback Europe/Berlin — gleiches Ergebnis
        monkeypatch.setattr(fo, '_fo_homebase', {})
        assert fo._fo_should_sync('AT-TZ', now=now) == (True, 'fast')
        # JFK-Homebase: dort ist es erst der 5.8. abends → Dienst am 7.8. ist
        # übermorgen ⇒ mid, kein fast
        monkeypatch.setattr(fo, '_fo_homebase', {'AT-TZ': 'JFK'})
        assert fo._fo_should_sync('AT-TZ', now=now) == (False, 'skip_mid')
        monkeypatch.setattr(fo, '_fo_last_sync', {'AT-TZ': now - 12 * 3600})
        assert fo._fo_should_sync('AT-TZ', now=now) == (True, 'mid')
    finally:
        if old_tz is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old_tz
        _time.tzset()


def _open_budget(monkeypatch):
    """Hintergrund-Budget im Test explizit öffnen (die echten Zähler-Reader
    tragen Modul-Memos, die aus fremden Tests stale sein können)."""
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: 0)
    monkeypatch.setattr(fo, '_lhfo_day_used', lambda: 0)


def test_refresh_all_defers_and_counts(monkeypatch):
    """Der 2-h-Lauf synct nur die fälligen Tokens und meldet deferred."""
    calls = []
    _open_budget(monkeypatch)
    monkeypatch.setattr(fo, '_fo_should_sync',
                        lambda tok, now=None: ((True, 'first') if tok == 'AT-A'
                                               else (False, 'too_soon')))
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


def test_refresh_all_books_skip_counters_and_classes(monkeypatch):
    """Transparenz für den Owner-Tagesreport: aufgeschobene Syncs landen
    GEBATCHT (ein Increment pro Grund und Lauf) in lhfo_skip:<grund>, und
    die done-Zeile trägt due_classes/defer_reasons."""
    booked = []
    _open_budget(monkeypatch)
    import blueprints.lh_open_api as lo
    monkeypatch.setattr(
        lo, 'budget_inc',
        lambda prefix, caller=None, units=1:
        booked.append((prefix, caller, units)))
    monkeypatch.setattr(fo, '_fo_should_sync',
                        lambda tok, now=None:
                        ((True, 'fast') if tok == 'AT-A'
                         else (False, 'skip_slow')))
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, 'flightops_import', lambda tok: ({}, 200))
    monkeypatch.setattr(fo, '_fo_mark_synced', lambda tok, now=None: None)
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_work(['AT-A', 'AT-B', 'AT-C'])
    assert booked == [('lhfo_skip', 'skip_slow', 2)]
    st = fo._refresh_all_state['last']
    assert st['due_classes'] == {'fast': 1}
    assert st['defer_reasons'] == {'skip_slow': 2}
    assert st['ok'] == 1 and st['deferred'] == 2


def test_refresh_all_orders_fast_before_slow(monkeypatch):
    """Priorisierung im Plan: fast_sb/fast → first → mid → slow. Wird das
    Budget knapp, trifft es zuerst die, denen Frische am wenigsten fehlt."""
    _open_budget(monkeypatch)
    reasons = {'AT-SLOW': 'slow', 'AT-SB': 'fast_sb', 'AT-MID': 'mid',
               'AT-NEW': 'first', 'AT-FAST': 'fast'}
    monkeypatch.setattr(fo, '_fo_should_sync',
                        lambda tok, now=None: (True, reasons[tok]))
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    calls = []
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    monkeypatch.setattr(fo, '_fo_mark_synced', lambda tok, now=None: None)
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_work(['AT-SLOW', 'AT-SB', 'AT-MID', 'AT-NEW', 'AT-FAST'])
    assert calls == ['AT-SB', 'AT-FAST', 'AT-NEW', 'AT-MID', 'AT-SLOW']


# ── 2b(ii). WELLEN-IMPORT (ersetzt den blinden 120-s-Demand-Vorlauf) ────────
def test_wave_import_follows_rotation(monkeypatch):
    """Pending-Grant wird beim Refresher angemeldet und in der NÄCHSTEN Welle
    importiert, sobald die (gefakte) Rotation durch ist — kein blindes
    Warten, kein Skip. Das ist der Fix für den 30.07.-Befund: 570 abgelaufene
    Grants, 120 s Wartezeit, 432 übersprungen — obwohl der Refresher sie
    Minuten später alle rotiert hatte."""
    state = {'AT-P': 'pending'}
    poked = []

    def _fake_rotate(tok):
        poked.append(tok)
        state[tok] = 'ok'          # „der Refresher hat rotiert"
        return True

    _open_budget(monkeypatch)
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
    st = fo._refresh_all_state['last']
    assert st['ok'] == 1 and st['waves'] >= 2


def test_wave_import_mixes_ready_and_rotated(monkeypatch):
    """Welle 1 importiert die schon gültigen Grants SOFORT (kein Warten auf
    die Rotation der anderen); der pending-Grant folgt in Welle 2."""
    state = {'AT-READY': 'ok', 'AT-LATE': 'pending'}
    _open_budget(monkeypatch)
    monkeypatch.setattr(fo, '_access_state',
                        lambda tok: (state.get(tok, 'ok'),
                                     None if state.get(tok) == 'pending'
                                     else 'ACC'))
    monkeypatch.setattr(fo, '_rotate_poke_remote',
                        lambda tok: state.__setitem__(tok, 'ok') or True)
    monkeypatch.setattr(fo, '_fo_should_sync', lambda tok, now=None: (True, 'x'))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    calls = []
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    try:
        fo._refresh_all_work(['AT-LATE', 'AT-READY'])
    finally:
        fo._refresher_demand.discard('AT-LATE')
    assert calls == ['AT-READY', 'AT-LATE']
    assert fo._refresh_all_state['last']['ok'] == 2


def test_wave_gives_up_on_stuck_rotation(monkeypatch):
    """Bleibt der Grant über alle Stall-Wellen pending, wird er übersprungen
    (Grund im Zähler) — NIEMALS selbst rotiert (der Refresher ist der einzige
    Rotierer), und der Lauf hängt nicht bis zur Deadline."""
    _open_budget(monkeypatch)
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
    assert st['skip_reasons'] == {'rotation_pending': 1}


# ── 3b. BUDGET-STOPP + FAIL-GRÜNDE im Lauf ──────────────────────────────────
def test_run_aborts_when_background_budget_closed(monkeypatch):
    """Tagesdeckel schon zu Laufbeginn erreicht ⇒ kein einziger Import, KEIN
    Demand an den Refresher (30.07.: hunderte Rotationen für Imports, die nie
    stattfanden) — alle User bleiben fällig, Grund steht im Zähler."""
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: 0)
    monkeypatch.setattr(fo, '_lhfo_day_used',
                        lambda: fo._LHFO_DAY_BACKGROUND_CEILING)
    monkeypatch.setattr(fo, '_fo_should_sync',
                        lambda tok, now=None: (True, 'duty_near'))
    calls, poked = [], []
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_rotate_poke_remote',
                        lambda tok: poked.append(tok) or True)
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_work(['AT-A', 'AT-B'])
    assert calls == [] and poked == []
    assert len(fo._refresher_demand) == 0
    st = fo._refresh_all_state['last']
    assert st['fail'] == 0
    assert st['skip_reasons'] == {'budget_day': 2}


def test_far_due_users_keep_day_reserve(monkeypatch):
    """slow-User (kein Dienst in Sicht) syncen NICHT mehr, sobald die
    Tages-Reserve für die Dienst-Klassen angebrochen wäre — fast-User
    laufen normal weiter."""
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: 0)
    monkeypatch.setattr(fo, '_lhfo_day_used',
                        lambda: (fo._LHFO_DAY_BACKGROUND_CEILING
                                 - fo._FO_FAR_DAY_HEADROOM))
    monkeypatch.setattr(
        fo, '_fo_should_sync',
        lambda tok, now=None: (True, 'slow' if tok == 'AT-FAR'
                               else 'fast'))
    calls = []
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, 'flightops_import',
                        lambda tok: calls.append(tok) or ({}, 200))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_work(['AT-FAR', 'AT-NEAR'])
    # Priorisierung: duty_near VOR far_due — und der far_due-User wird mit
    # Grund übersprungen statt zu failen.
    assert calls == ['AT-NEAR']
    st = fo._refresh_all_state['last']
    assert st['ok'] == 1
    assert st['skip_reasons'] == {'budget_day_far_reserve': 1}


def test_fail_reasons_are_counted(monkeypatch):
    """Jeder fail trägt seinen Grund in die done-Zeile — „228 fail und
    niemand weiß warum" (30.07.) darf es nicht mehr geben."""
    _open_budget(monkeypatch)
    monkeypatch.setattr(fo, '_fo_should_sync', lambda tok, now=None: (True, 'x'))
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))

    class _Resp:
        def get_json(self, silent=False):
            return {'ok': False, 'error': 'duty_events_failed'}

    monkeypatch.setattr(fo, 'flightops_import', lambda tok: (_Resp(), 502))
    monkeypatch.setattr(fo.time, 'sleep', lambda s: None)
    fo._refresh_all_state['running'] = True
    fo._refresh_all_state['drain'] = False
    fo._refresh_all_work(['AT-E1', 'AT-E2'])
    st = fo._refresh_all_state['last']
    assert st['fail'] == 2
    assert st['fail_reasons'] == {'duty_events_failed': 2}


# ── 3c. KADENZ-PERSISTENZ (deferred=0-Loch vom 30.07.) ──────────────────────
def test_mark_synced_writes_durable_stamp(monkeypatch):
    """Erfolgs-Stempel landet zusätzlich als metadata.fo_bg_sync_at im Profil
    (atomarer Merge) — damit überlebt die Kadenz Worker-Restarts/Deploys."""
    import sys as _sys
    merged = {}
    _mods = [app]
    _cur = _sys.modules.get('app')
    if _cur is not None and _cur is not app:
        _mods.append(_cur)
    for _m in _mods:            # beide app-Kopien (s. _patch_briefings-Banner)
        monkeypatch.setattr(_m, 'SB_AVAILABLE', True, raising=False)
        monkeypatch.setattr(
            _m, '_profile_metadata_merge_sb',
            lambda tok, patch: merged.update({tok: patch}) or True,
            raising=False)
    fo._fo_mark_synced('AT-STAMP', now=1_700_000_000.5)
    assert fo._fo_last_sync['AT-STAMP'] == 1_700_000_000.5
    assert merged == {'AT-STAMP': {'fo_bg_sync_at': 1_700_000_000}}


def test_hydrate_stamp_restores_cadence_after_restart():
    """DB-Stempel füllt die leere Prozess-Map (Neustart) — aber nie rückwärts
    und nie mit Müll/Zukunfts-Werten."""
    now = 1_700_000_000.0
    fo._fo_hydrate_stamp('AT-H', now - 3600, now=now)
    assert fo._fo_last_sync['AT-H'] == now - 3600
    # Neuerer Prozess-Stand gewinnt gegen älteren DB-Stempel
    fo._fo_last_sync['AT-H'] = now - 60
    fo._fo_hydrate_stamp('AT-H', now - 3600, now=now)
    assert fo._fo_last_sync['AT-H'] == now - 60
    # Müll und Zukunft werden verworfen
    fo._fo_hydrate_stamp('AT-H2', None, now=now)
    fo._fo_hydrate_stamp('AT-H3', 'kaputt', now=now)
    fo._fo_hydrate_stamp('AT-H4', now + 7200, now=now)
    for t in ('AT-H2', 'AT-H3', 'AT-H4'):
        assert t not in fo._fo_last_sync
    # …und mit gefüllter Map ist der Token im nächsten Lauf deferred
    assert fo._fo_should_sync('AT-H', now=now) == (False, 'too_soon')


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
