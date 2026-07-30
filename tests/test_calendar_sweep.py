"""Kalender-Sweep (blueprints/calendar_sweep.py) — periodischer Nachlauf für
gespeicherte iCal-Links.

Der Schwerpunkt liegt auf dem HOST-FILTER: Lufthansa-Links (myTime,
810 von 1038) dürfen NIE server-seitig gesweept werden (LH-Warnung
2026-07-21). Der Filter ist eine AUSSCHLUSS-Liste — neue Anbieter fallen
also nicht still hinten runter — und muss Täusch-Hosts
(`api.lufthansa.com.evil.tld`) wie LH behandeln, ohne harmlose Namen
(`notlufthansa.com`) fälschlich zu sperren.
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import calendar_sweep as cs


# ── Host-Extraktion ─────────────────────────────────────────────────────────

@pytest.mark.parametrize('url,expected', [
    ('https://api.lufthansa.com/mytime/roster.ics', 'api.lufthansa.com'),
    ('webcal://api.lufthansa.com/mytime/roster.ics', 'api.lufthansa.com'),
    ('webcals://API.Lufthansa.COM/x.ics', 'api.lufthansa.com'),
    ('https://cube.aero/ical?key=1', 'cube.aero'),
    ('https://schedule.swiss.com:443/roster.ics', 'schedule.swiss.com'),
    ('https://user:pw@outlook.office365.com/owa/cal.ics', 'outlook.office365.com'),
    ('https://p160-caldav.icloud.com./published/2/abc', 'p160-caldav.icloud.com'),
    ('https://[2001:db8::1]/roster.ics', '2001:db8::1'),
    ('  https://offblock.de/feed.ics  ', 'offblock.de'),
])
def test_feed_host_parst_erwartete_hosts(url, expected):
    assert cs.feed_host(url) == expected


@pytest.mark.parametrize('url', [
    '',
    None,
    'api.lufthansa.com/mytime/roster.ics',        # kein Scheme
    'ftp://cube.aero/x.ics',
    'file:///etc/passwd',
    'javascript:alert(1)',
    'https://',
    'https:///pfad-ohne-host',
    'https://api.lufthansa.com\\@evil.tld/x.ics',  # Backslash-Täuschung
])
def test_feed_host_unklar_ist_leer(url):
    assert cs.feed_host(url) == ''


def test_unsichtbare_zeichen_im_link_verstecken_den_host_nicht():
    # Root-Cause Johanna 2026-07-15: aus myTime kopierte Links tragen
    # Leerzeichen/Zero-Width mitten in URL — der Filter muss denselben Host
    # sehen wie der spätere Abruf (app._sanitize_feed_url).
    tricky = 'webcal://api.​lufthansa.com/ mytime/ roster.ics'
    assert cs.feed_host(tricky) == 'api.lufthansa.com'
    assert cs.sweep_allows_url(tricky) is False


# ── Ausschluss-Liste ────────────────────────────────────────────────────────

@pytest.mark.parametrize('host', [
    'lufthansa.com',
    'api.lufthansa.com',
    'API.LUFTHANSA.COM',
    'mytime.api.lufthansa.com',
    'api.lufthansa.com.',
    'api.lufthansa.com.evil.tld',      # Täusch-Host: LH-Labels drin
    'lufthansa.com.mirror.example',
])
def test_lufthansa_hosts_sind_blockiert(host):
    assert cs.host_is_blocked(host) is True


@pytest.mark.parametrize('host', [
    'notlufthansa.com',                # kein Label-Treffer
    'mylufthansa.com',
    'lufthansa.company',               # zweites Label ist NICHT 'com'
    'lufthansa-group.example',
    'cube.aero', 'www.cube.aero',
    'schedule.swiss.com',
    'outlook.office365.com',
    'offblock.de',
    'flybase.eurowings.com',
    'crewaccess.cms.discover.aero',
    'p160-caldav.icloud.com',
    'ecrew.germanairways.com',
    'apps.apple.com',
])
def test_nicht_lh_hosts_bleiben_erlaubt(host):
    assert cs.host_is_blocked(host) is False


@pytest.mark.parametrize('host', ['', None, '   ', '.'])
def test_unbekannter_host_ist_fail_closed(host):
    assert cs.host_is_blocked(host) is True


@pytest.mark.parametrize('host', ['www.ui-deref.de', 'ui-deref.de', 'supr.sh'])
def test_redirector_hosts_sind_gesperrt(host):
    # Ziel unsichtbar bzw. nachweislich myTime → fail-closed.
    assert cs.host_is_blocked(host) is True


def test_filter_ist_ausschluss_liste_kein_whitelist():
    # Ein Anbieter, den heute niemand kennt, muss OHNE Code-Änderung
    # mitgesweept werden — sonst fällt er still hinten runter.
    assert cs.sweep_allows_url('https://roster.brandneue-airline.example/f.ics') is True


def test_env_kann_weitere_hosts_sperren(monkeypatch):
    monkeypatch.setenv('AEROX_SWEEP_BLOCK_HOSTS', 'roster.example, foo.test')
    assert cs.host_is_blocked('a.roster.example') is True
    assert cs.host_is_blocked('foo.test') is True
    assert cs.host_is_blocked('cube.aero') is False


# ── Versteckte myTime-Ziele (Redirect-/Deref-Links) ─────────────────────────

def test_deref_link_mit_lh_ziel_ist_gesperrt():
    # Prod-Messung 30.07.: 4 User haben genau diese Form gespeichert. Host
    # www.ui-deref.de ist harmlos, das Ziel ist api.lufthansa.com — der Abruf
    # folgt Redirects, ein reiner Host-Check hätte myTime gezogen.
    u = ('https://www.ui-deref.de/r/?to=https%3A%2F%2Fapi.lufthansa.com'
         '%2Fmytime%2Frostershareinfo%2FdownloadRoster%3Fapi_key%3Dxxx&tt1=abc')
    assert cs.url_hides_blocked_host(u) is True
    assert cs.sweep_allows_url(u) is False


def test_lh_ziel_auch_unkodiert_und_doppelt_kodiert_erkannt():
    assert cs.sweep_allows_url(
        'https://redirect.example/go?to=https://api.lufthansa.com/mytime/r') is False
    assert cs.sweep_allows_url(
        'https://redirect.example/go?to=https%253A%252F%252Fapi.lufthansa.com'
        '%252Fmytime') is False


def test_link_kuerzer_ohne_sichtbares_ziel_ist_fail_closed():
    # supr.sh (1 User): Ziel prinzipiell unsichtbar → könnte myTime sein.
    assert cs.sweep_allows_url('https://supr.sh/i/dxmpiejJNZ.ics') is False


def test_harmlose_urls_werden_vom_ziel_scan_nicht_getroffen():
    for u in ('https://cube.aero/account/ical/03627d1470ff258c9d868982854deb',
              'https://schedule.swiss.com/1/2/36d546e9-d93c.ics',
              'https://offblock.de/de/ical/ffd64257-8040.ics',
              'https://notlufthansa.com/r.ics',
              'https://outlook.office365.com/owa/calendar/x@flyedelweiss.ch/r.ics'):
        assert cs.url_hides_blocked_host(u) is False, u
        assert cs.sweep_allows_url(u) is True, u


@pytest.mark.parametrize('url,allowed', [
    ('https://api.lufthansa.com/mytime/roster.ics', False),
    ('webcal://api.lufthansa.com/mytime/roster.ics', False),
    ('WEBCAL://Api.Lufthansa.Com/mytime/roster.ics', False),
    ('https://cube.aero/ical?key=1', True),
    ('webcal://schedule.swiss.com/r.ics', True),
    ('https://notlufthansa.com/r.ics', True),
    ('https://api.lufthansa.com.evil.tld/r.ics', False),
    ('kaputt', False),
])
def test_sweep_allows_url(url, allowed):
    assert cs.sweep_allows_url(url) is allowed


def test_zweit_link_lufthansa_sperrt_den_ganzen_user():
    # Der Import-Endpoint zieht ohne expliziten url_2 IMMER den gespeicherten
    # Zweit-Link nach — ein LH-Link in Slot 2 wäre also doch ein myTime-Abruf.
    ok = {'token': 'AT-1', 'url': 'https://cube.aero/a.ics', 'url_2': '', 'age_h': 30}
    lh2 = dict(ok, url_2='webcal://api.lufthansa.com/mytime/off.ics')
    assert cs._candidate_allowed(ok) is True
    assert cs._candidate_allowed(lh2) is False


# ── Lauf-Verhalten ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _kein_schlafen(monkeypatch):
    monkeypatch.setattr(cs, '_SWEEP_GAP_S', 0)
    cs._sweep_state.update({'running': False, 'drain': False, 'last': None,
                            'started_at': 0.0})
    yield
    cs._sweep_state.update({'running': False, 'drain': False, 'last': None,
                            'started_at': 0.0})


def _fake_app(monkeypatch, calls, boom_on=()):
    """Minimal-Stub des app-Moduls: _sweep_work macht `import app`."""
    mod = types.ModuleType('app')

    def _refresh(token, base_url=None):
        if token in boom_on:
            raise RuntimeError('kaputt')
        calls.append((token, base_url))

    mod._maybe_refresh_calendar_feed = _refresh
    mod._server_ical_refresh_enabled = lambda: True
    mod.SB_AVAILABLE = False
    mod.sb = None
    monkeypatch.setitem(sys.modules, 'app', mod)
    return mod


def test_lauf_ueberspringt_lufthansa_und_stoesst_den_rest_an(monkeypatch):
    calls = []
    _fake_app(monkeypatch, calls)
    cands = [
        {'token': 'AT-LH', 'url': 'webcal://api.lufthansa.com/mytime/r.ics',
         'url_2': '', 'age_h': 300},
        {'token': 'AT-CUBE', 'url': 'https://cube.aero/r.ics',
         'url_2': '', 'age_h': 40},
        {'token': 'AT-SWISS', 'url': 'https://schedule.swiss.com/r.ics',
         'url_2': '', 'age_h': 2},
        {'token': 'AT-BAD', 'url': 'nicht-mal-eine-url', 'url_2': '', 'age_h': 5},
    ]
    cs._sweep_work(cands, 'https://api.aerosteuer.de')
    assert [c[0] for c in calls] == ['AT-CUBE', 'AT-SWISS']
    assert all(c[1] == 'https://api.aerosteuer.de' for c in calls)
    last = cs._sweep_state['last']
    assert last['checked'] == 4 and last['handed'] == 2
    assert last['skipped_host'] == 2 and last['errors'] == 0
    assert last['stale24'] == 1          # nur AT-CUBE ist >24 h alt
    assert cs._sweep_state['running'] is False


def test_ein_fehler_bricht_den_lauf_nie_ab(monkeypatch):
    calls = []
    _fake_app(monkeypatch, calls, boom_on=('AT-2',))
    cands = [{'token': f'AT-{i}', 'url': 'https://cube.aero/r.ics',
              'url_2': '', 'age_h': 10} for i in (1, 2, 3)]
    cs._sweep_work(cands, 'https://x.example')
    assert [c[0] for c in calls] == ['AT-1', 'AT-3']
    assert cs._sweep_state['last']['errors'] == 1
    assert cs._sweep_state['last']['checked'] == 3


def test_drain_stoppt_zwischen_zwei_usern(monkeypatch):
    calls = []
    mod = _fake_app(monkeypatch, calls)

    def _refresh(token, base_url=None):
        calls.append((token, base_url))
        cs._sweep_state['drain'] = True      # Deploy/Worker-Recycle

    mod._maybe_refresh_calendar_feed = _refresh
    cands = [{'token': f'AT-{i}', 'url': 'https://cube.aero/r.ics',
              'url_2': '', 'age_h': 10} for i in range(5)]
    cs._sweep_work(cands, 'https://x.example')
    assert len(calls) == 1


def test_endpoint_laeuft_nie_zweimal_parallel(monkeypatch):
    calls = []
    _fake_app(monkeypatch, calls)
    monkeypatch.setattr(cs, 'calendar_feed_candidates',
                        lambda *a, **k: ([{'token': 'AT-1',
                                           'url': 'https://cube.aero/r.ics',
                                           'url_2': '', 'age_h': 10}],
                                         {'rows': 3, 'skipped_host': 2}))
    started = []
    monkeypatch.setattr(cs, '_start_sweep',
                        lambda cands, base, stats=None: started.append(len(cands)))
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(cs.calendar_sweep_bp)
    client = app.test_client()

    r1 = client.post('/api/internal/calendar/sweep')
    assert r1.get_json().get('started') is True and started == [1]
    assert r1.get_json().get('skipped_host') == 2
    # running-Flag steht noch (der Thread ist gestubbt) → zweiter Aufruf
    # startet NICHTS.
    r2 = client.post('/api/internal/calendar/sweep')
    assert r2.get_json().get('already_running') is True
    assert started == [1]


def test_haengender_lauf_blockiert_nicht_fuer_immer(monkeypatch):
    """Harter Worker-Kill mitten im Lauf ließ running=True stehen — ohne
    Selbstheilung wäre der Sweep danach dauerhaft tot (und niemand merkte es)."""
    calls = []
    _fake_app(monkeypatch, calls)
    monkeypatch.setattr(cs, 'calendar_feed_candidates',
                        lambda *a, **k: ([{'token': 'AT-1',
                                           'url': 'https://cube.aero/r.ics',
                                           'url_2': '', 'age_h': 10}],
                                         {'rows': 1, 'skipped_host': 0}))
    started = []
    monkeypatch.setattr(cs, '_start_sweep',
                        lambda cands, base, stats=None: started.append(1))
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)
    cs._sweep_state.update({'running': True,
                            'started_at': __import__('time').time() - 3 * 3600})
    cs._sweep_thread[0] = None

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(cs.calendar_sweep_bp)
    r = app.test_client().post('/api/internal/calendar/sweep')
    assert r.get_json().get('started') is True and started == [1]


def test_kill_switch_stoppt_den_sweep(monkeypatch):
    calls = []
    mod = _fake_app(monkeypatch, calls)
    mod._server_ical_refresh_enabled = lambda: False
    monkeypatch.setattr(cs, 'calendar_feed_candidates',
                        lambda *a, **k: ([{'token': 'AT-1',
                                           'url': 'https://cube.aero/r.ics',
                                           'url_2': '', 'age_h': 99}], {}))
    started = []
    monkeypatch.setattr(cs, '_start_sweep',
                        lambda cands, base, stats=None: started.append(1))
    monkeypatch.delenv('ADSB_POLL_SECRET', raising=False)

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(cs.calendar_sweep_bp)
    r = app.test_client().post('/api/internal/calendar/sweep')
    body = r.get_json()
    assert body['ok'] is True and body['skipped'] == 'server_ical_refresh_off'
    assert started == [] and calls == []
    assert cs._sweep_state['running'] is False


def test_endpoint_braucht_das_poll_secret(monkeypatch):
    monkeypatch.setenv('ADSB_POLL_SECRET', 'geheim')
    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(cs.calendar_sweep_bp)
    client = app.test_client()
    assert client.post('/api/internal/calendar/sweep').status_code == 403
    assert client.get('/api/internal/calendar/sweep-status').status_code == 403
    ok = client.get('/api/internal/calendar/sweep-status',
                    headers={'X-Poll-Secret': 'geheim'})
    assert ok.status_code == 200 and ok.get_json()['ok'] is True


def test_self_base_url_wird_immer_https(monkeypatch):
    monkeypatch.delenv('AEROX_SELF_BASE_URL', raising=False)
    assert cs._self_base_url() == 'https://api.aerosteuer.de'
    monkeypatch.setenv('AEROX_SELF_BASE_URL', 'http://api.aerosteuer.de/')
    assert cs._self_base_url() == 'https://api.aerosteuer.de'
    monkeypatch.setenv('AEROX_SELF_BASE_URL', 'staging.example')
    assert cs._self_base_url() == 'https://staging.example'


def test_kandidaten_aelteste_zuerst_lh_raus_und_ohne_sb_leer(monkeypatch):
    calls = []
    _fake_app(monkeypatch, calls)      # SB_AVAILABLE = False
    assert cs.calendar_feed_candidates() == ([], {'rows': 0, 'skipped_host': 0})

    class _Res:
        def __init__(self, data):
            self.data = data

    class _Q:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def range(self, lo, hi):
            self._slice = (lo, hi)
            return self

        def execute(self):
            lo, hi = self._slice
            return _Res(self.rows[lo:hi + 1])

    rows = [
        {'token': 'AT-NEU', 'metadata': {'calendar_feed': {
            'url': 'https://cube.aero/a.ics',
            'imported_at': '2999-01-01T00:00:00'}}},
        {'token': 'AT-ALT', 'metadata': {'calendar_feed': {
            'url': 'https://cube.aero/b.ics', 'imported_at': '2020-01-01T00:00:00'}}},
        {'token': 'AT-NIE', 'metadata': {'calendar_feed': {
            'url': 'https://cube.aero/c.ics'}}},
        {'token': 'AT-OHNE-URL', 'metadata': {'calendar_feed': {}}},
        # myTime — muss VOR dem Deckel rausfliegen, sonst frisst LH (950 von
        # ~1170 Links) den Lauf auf und echte Kandidaten bleiben liegen.
        {'token': 'AT-LH', 'metadata': {'calendar_feed': {
            'url': 'webcal://api.lufthansa.com/mytime/r.ics',
            'imported_at': '2019-01-01T00:00:00'}}},
    ]
    mod = sys.modules['app']
    mod.SB_AVAILABLE = True
    mod.sb = types.SimpleNamespace(table=lambda name: _Q(rows))
    got, stats = cs.calendar_feed_candidates()
    assert [c['token'] for c in got] == ['AT-NIE', 'AT-ALT', 'AT-NEU']
    assert stats == {'rows': 4, 'skipped_host': 1}


def test_schmaler_select_wird_gelesen_und_bei_fehler_gefallbackt(monkeypatch):
    """Prod liest die Feed-URL per JSON-Pfad (metadata enthält sonst auch die
    geparsten Events + FlightOps-Tokens). Beide Zeilen-Formen müssen gehen."""
    calls = []
    _fake_app(monkeypatch, calls)
    narrow = [{'token': 'AT-N', 'feed_url': 'https://cube.aero/n.ics',
               'feed_imported_at': '2020-01-01T00:00:00', 'feed2_url': None},
              {'token': 'AT-LH', 'feed_url': 'https://api.lufthansa.com/m/r.ics',
               'feed_imported_at': None, 'feed2_url': None}]
    wide = [{'token': 'AT-W', 'metadata': {'calendar_feed': {
        'url': 'https://offblock.de/w.ics'}}}]
    seen = []

    class _Q:
        def select(self, expr, *a, **k):
            seen.append(expr)
            self.expr = expr
            return self

        def filter(self, *a, **k):
            return self

        def range(self, lo, hi):
            return self

        def execute(self):
            if 'metadata->calendar_feed->>url' in self.expr:
                if seen.count(self.expr) == 1:
                    return types.SimpleNamespace(data=narrow)
                raise RuntimeError('PGRST100')
            return types.SimpleNamespace(data=wide)

    mod = sys.modules['app']
    mod.SB_AVAILABLE = True
    mod.sb = types.SimpleNamespace(table=lambda name: _Q())
    got, stats = cs.calendar_feed_candidates()
    assert [c['token'] for c in got] == ['AT-N']
    assert stats == {'rows': 2, 'skipped_host': 1}
    assert seen[0] == cs._SELECT_NARROW

    # Zweiter Lauf: der schmale Select failt → Fallback auf metadata.
    got2, stats2 = cs.calendar_feed_candidates()
    assert [c['token'] for c in got2] == ['AT-W']
    assert cs._SELECT_WIDE in seen


def test_deckel_frisst_keine_erlaubten_kandidaten(monkeypatch):
    """Regressions-Gate: der Host-Filter muss VOR dem limit greifen."""
    calls = []
    _fake_app(monkeypatch, calls)

    class _Q:
        def __init__(self, rows):
            self.rows = rows

        def select(self, *a, **k):
            return self

        def filter(self, *a, **k):
            return self

        def range(self, lo, hi):
            self._slice = (lo, hi)
            return self

        def execute(self):
            lo, hi = self._slice
            return types.SimpleNamespace(data=self.rows[lo:hi + 1])

    rows = [{'token': f'AT-LH{i}', 'metadata': {'calendar_feed': {
        'url': 'https://api.lufthansa.com/mytime/r.ics',
        'imported_at': '2019-01-01T00:00:00'}}} for i in range(50)]
    rows.append({'token': 'AT-CUBE', 'metadata': {'calendar_feed': {
        'url': 'https://cube.aero/r.ics', 'imported_at': '2026-07-30T00:00:00'}}})
    mod = sys.modules['app']
    mod.SB_AVAILABLE = True
    mod.sb = types.SimpleNamespace(table=lambda name: _Q(rows))
    got, stats = cs.calendar_feed_candidates(limit=5)
    assert [c['token'] for c in got] == ['AT-CUBE']
    assert stats['skipped_host'] == 50
