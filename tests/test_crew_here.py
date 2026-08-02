"""„WER IST GERADE HIER" — die Matching-Grundlage für Hangouts (Owner 2026-07-29).

„Hangouts werden kaum genutzt. Demografie-Filter sind vom Tisch — AeroX kennt
die Roster, nutz DIE." Dieser Test deckt die operativen Roster-Fakten und den
Endpoint /api/user/crew-here ab:

  * `_crew_no_early_start` — die TRI-STATE-Schwellen (Pickup < 08:00,
    Dienstbeginn < 10:00), inkl. „ich weiss es nicht" (None)
  * `_crew_nights_at` — verbleibende zusammenhängende Nächte an einer Station
  * `_crew_ops_facts` — here / hotel / nights / arriving / departing
  * der Endpoint: ehrliche Zahlen, Sichtbarkeits-Gates, und die
    Redaktionsregeln (KEIN fremdes Token, KEIN fremder Hotelname)

SICHERHEIT: kein echter SB-Call — alle Loader sind gemockt.
"""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
from unittest.mock import patch

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


TODAY = '2026-07-29'
TOMORROW = '2026-07-30'
VIEWER = 'AT-VIEWER'
LH_PROFILE = {'name': 'Miguel', 'airline': 'Lufthansa', 'homebase': 'FRA'}


def _day(datum, place=None, start=None, pickup=None, sectors=None):
    """Ein Roster-Tag, so schlank wie die echten Snapshot-Tage."""
    d = {'datum': datum, 'reader_facts': {}}
    if place:
        d['reader_facts']['layover_ort'] = place
    if start:
        d['reader_facts']['start_time'] = start
    if pickup:
        d['marker'] = f'Pickup {pickup}'
    if sectors:
        d['ical_sectors'] = sectors
    return d


FLIGHT = [{'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
           'dep_iso': '2026-07-30T10:00:00Z', 'arr_iso': '2026-07-30T18:00:00Z'}]


# ── Schwellen: „keine frühe Verpflichtung morgen früh" ──────────────────────

def test_kein_roster_tag_ist_unbekannt_nicht_frei():
    """None heisst „wir wissen es nicht" — und zählt nirgends mit."""
    assert A._crew_no_early_start(None) is None
    assert A._crew_no_early_start('kaputt') is None


def test_frueher_pickup_vor_0800_ist_nicht_frei():
    assert A._crew_no_early_start(_day(TOMORROW, pickup='0530')) is False
    # exakt auf der Schwelle ist NICHT früh
    assert A._crew_no_early_start(_day(TOMORROW, pickup='0800')) is True


def test_frueher_dienstbeginn_vor_1000_ist_nicht_frei():
    assert A._crew_no_early_start(_day(TOMORROW, start='05:50')) is False
    assert A._crew_no_early_start(_day(TOMORROW, start='09:59')) is False
    assert A._crew_no_early_start(_day(TOMORROW, start='10:00')) is True


def test_spaeter_dienst_ist_frei():
    assert A._crew_no_early_start(_day(TOMORROW, start='14:00')) is True


def test_tag_ohne_zeiten_und_ohne_fluege_ist_frei():
    """Frei-Tag / Urlaub / reiner Layover-Ruhetag."""
    assert A._crew_no_early_start(_day(TOMORROW, place='BKK')) is True


def test_fluege_ohne_zeiten_sind_unbekannt_nicht_frei():
    """Wir WISSEN, dass geflogen wird — nur nicht wann. „Frei" wäre gelogen."""
    assert A._crew_no_early_start(_day(TOMORROW, sectors=FLIGHT)) is None


def test_pickup_und_dienstbeginn_sind_zwei_eigene_schwellen():
    """Pickup 08:30 heisst Dienst ~09:30 — das ist KEIN „früher Dienst". Liefe
    der Pickup als Dienstbeginn-Kandidat mit, wäre die 08:00-Schwelle
    wirkungslos (alles unter 08:00 liegt ohnehin unter 10:00)."""
    assert A._crew_no_early_start(_day(TOMORROW, pickup='0830')) is True
    assert A._crew_no_early_start(_day(TOMORROW, pickup='0759')) is False


def test_frueher_pickup_schlaegt_spaeten_start():
    """Der Pickup ist die echte Weckzeit — er gewinnt gegen eine späte
    Dienstbeginn-Angabe."""
    d = _day(TOMORROW, start='14:00', pickup='0600')
    assert A._crew_no_early_start(d) is False


# ── Verbleibende Nächte ─────────────────────────────────────────────────────

def _days(*pairs):
    return {datum: _day(datum, place=place) for datum, place in pairs}


def test_nights_zaehlt_zusammenhaengende_tage():
    days = _days(('2026-07-29', 'BKK'), ('2026-07-30', 'BKK'),
                 ('2026-07-31', 'FRA'))
    assert A._crew_nights_at(days, 'BKK', '2026-07-29') == 2
    assert A._crew_nights_at(days, 'BKK', '2026-07-30') == 1


def test_nights_null_wenn_gar_nicht_dort():
    days = _days(('2026-07-29', 'FRA'))
    assert A._crew_nights_at(days, 'BKK', '2026-07-29') == 0


def test_nights_luecke_bricht_die_kette():
    """Ein fehlender Roster-Tag ist KEINE stille Fortsetzung."""
    days = _days(('2026-07-29', 'BKK'), ('2026-07-31', 'BKK'))
    assert A._crew_nights_at(days, 'BKK', '2026-07-29') == 1


def test_nights_gedeckelt():
    days = {A._crew_day_shift('2026-07-01', i): _day('x', place='FRA')
            for i in range(40)}
    assert A._crew_nights_at(days, 'FRA', '2026-07-01') == A._CREW_OPS_MAX_NIGHTS


# ── Gesamt-Fakten ───────────────────────────────────────────────────────────

def _ops(iata='BKK', ref=TODAY, days=None, hotel='Novotel BKK'):
    days = days if days is not None else _days(
        ('2026-07-28', 'FRA'), ('2026-07-29', 'BKK'), ('2026-07-30', 'BKK'),
        ('2026-07-31', 'FRA'))
    with patch.object(A, '_crew_hotel_at', return_value=hotel):
        return A._crew_ops_facts(VIEWER, iata, ref, profile=LH_PROFILE,
                                 days=days)


def test_ops_facts_layover_zwei_naechte():
    f = _ops()
    assert f['here'] is True
    assert f['place'] == 'BKK'
    assert f['hotel'] == 'Novotel BKK'
    assert f['nights'] == 2
    assert f['arriving_today'] is True        # gestern noch FRA
    assert f['departing_tomorrow'] is False   # morgen noch BKK
    assert f['free_tomorrow'] is True         # 30.07. ohne Zeiten/Flüge


def test_ops_facts_wer_nicht_da_ist_behauptet_nichts():
    """Kein Fakt über eine Station, an der der User gar nicht steht."""
    f = _ops(iata='JFK')
    assert f['here'] is False
    assert f['hotel'] is None
    assert f['nights'] == 0
    assert f['free_tomorrow'] is None
    assert f['arriving_today'] is None
    assert f['departing_tomorrow'] is None


def test_ops_facts_ohne_roster_ist_alles_unbekannt():
    f = _ops(days={})
    assert f['here'] is False
    assert f['place'] is None
    assert f['nights'] == 0


def test_ops_facts_arriving_unbekannt_ohne_vortag():
    days = _days(('2026-07-29', 'BKK'))
    f = _ops(days=days)
    assert f['here'] is True
    assert f['arriving_today'] is None       # kein Vortag im Roster
    assert f['departing_tomorrow'] is None   # kein Folgetag im Roster


# ── Endpoint /api/user/crew-here ────────────────────────────────────────────

def _member(token, hotel=None, free=True, nights=1, arriving=True,
            departing=False, name='Crew'):
    return {'token': token,
            'profile': {'name': name, 'avatar_url': f'https://x/{token}.jpg'},
            'ops': {'here': True, 'place': 'BKK', 'hotel': hotel,
                    'nights': nights, 'free_tomorrow': free,
                    'arriving_today': arriving,
                    'departing_tomorrow': departing}}


def _crew_here(members, my_days=None, my_hotel='Novotel BKK', friends=(),
               iata='BKK', token=VIEWER):
    my_days = my_days if my_days is not None else _days(
        ('2026-07-28', 'FRA'), ('2026-07-29', 'BKK'), ('2026-07-30', 'BKK'))
    with patch.object(A, '_crew_ops_today', return_value=TODAY), \
         patch.object(A, '_crew_roster_days', return_value=my_days), \
         patch.object(A, '_profile_load', return_value={'profile': LH_PROFILE}), \
         patch.object(A, '_friends_load', return_value={'friends': list(friends)}), \
         patch.object(A, '_crew_hotel_at', return_value=my_hotel), \
         patch.object(A, '_crew_here_members', return_value=members), \
         A.app.test_request_context(f'/api/user/crew-here/{token}?iata={iata}'):
        return A.get_crew_here(token).get_json()


def test_crew_here_zaehlt_ehrlich():
    data = _crew_here([
        _member('AT-A', hotel='Novotel BKK', free=True, nights=2),
        _member('AT-B', hotel='Novotel BKK', free=False, nights=1),
        _member('AT-C', hotel='Marriott BKK', free=True, nights=3),
        _member('AT-D', hotel=None, free=None, nights=1),
    ])
    assert data['ok'] is True
    assert data['iata'] == 'BKK'
    assert data['here'] is True
    assert data['counts'] == {
        'total': 4, 'same_hotel': 2, 'free_tomorrow': 2,
        'staying_2plus': 2, 'arriving_today': 4, 'departing_tomorrow': 0}


def test_crew_here_unbekanntes_free_zaehlt_nicht_als_frei():
    data = _crew_here([_member('AT-A', free=None)], friends=['AT-A'])
    assert data['counts']['free_tomorrow'] == 0
    assert data['crew'][0]['free_tomorrow'] is None


def test_crew_here_ohne_eigenes_hotel_kein_same_hotel():
    """Kennt das Verzeichnis MEIN Hotel nicht, behaupten wir keine Treffer."""
    data = _crew_here([_member('AT-A', hotel='Novotel BKK')], my_hotel=None,
                      friends=['AT-A'])
    assert data['counts']['same_hotel'] == 0
    assert data['crew'][0]['same_hotel'] is False
    assert data['my_hotel'] is None


def test_crew_here_nicht_freunde_bleiben_anonym():
    """DATENSCHUTZ-KONTRAKT (Owner 2026-08-02 „Tibor, Jennifer und 94 andere
    — aber ohne zu sagen, ob die frei haben und wer sie sind"): Nicht-Freunde
    erscheinen NIE als Person (kein Name, kein Avatar, keine Flags) — sie
    zählen nur in den anonymen Aggregaten."""
    data = _crew_here([_member('AT-A', name='Fremde Person'),
                       _member('AT-FRIEND', name='Frida')],
                      friends=['AT-FRIEND'])
    assert data['counts']['total'] == 2
    assert data['friends_here'] == 1
    assert [c['name'] for c in data['crew']] == ['Frida']
    blob = str(data)
    assert 'Fremde Person' not in blob
    assert 'AT-A' not in blob


def test_crew_here_liefert_keine_fremden_tokens():
    """Das Token IST das Bearer-Credential — Fremde stehen seit der
    Datenschutz-Runde 2026-08-02 gar nicht mehr in der Liste."""
    data = _crew_here([_member('AT-A'), _member('AT-FRIEND')],
                      friends=['AT-FRIEND'])
    tokens = {c['token'] for c in data['crew']}
    assert 'AT-A' not in tokens
    assert 'AT-FRIEND' in tokens
    assert all(c['match_id'] for c in data['crew'])


def test_crew_here_liefert_keinen_fremden_hotelnamen():
    """Crew-Hotels sind airline-vertraulich (vgl. _filter_crew_hotels) — nach
    aussen geht nur der abgeleitete Boolean (auch bei Freunden)."""
    data = _crew_here([_member('AT-A', hotel='Novotel BKK')], friends=['AT-A'])
    assert 'hotel' not in data['crew'][0]
    assert data['crew'][0]['same_hotel'] is True
    assert data['my_hotel'] == 'Novotel BKK'   # das eigene darf man sehen


def test_crew_here_ohne_bekannten_ort_sagt_das_ehrlich():
    data = _crew_here([], my_days={}, iata='')
    assert data['ok'] is True
    assert data['reason'] == 'no_location'
    assert data['counts']['total'] == 0


def test_crew_here_vorschau_ist_gedeckelt():
    members = [_member(f'AT-{i}') for i in range(30)]
    data = _crew_here(members, friends=[f'AT-{i}' for i in range(30)])
    assert data['counts']['total'] == 30
    assert len(data['crew']) == A._CREW_HERE_PREVIEW_MAX
    assert data['crew_truncated'] is True


def test_crew_here_sortiert_selbes_hotel_nach_vorn():
    """Sortierung innerhalb der (reinen Freundes-)Liste: selbes Hotel zuerst,
    dann längster Aufenthalt. Nicht-Freunde (Xaver) fehlen komplett."""
    data = _crew_here([
        _member('AT-X', hotel=None, nights=1, name='Xaver'),
        _member('AT-HOTEL', hotel='Novotel BKK', nights=1, name='Hanna'),
        _member('AT-FRIEND', hotel=None, nights=3, name='Frida'),
    ], friends=['AT-FRIEND', 'AT-HOTEL'])
    assert [c['name'] for c in data['crew']] == ['Hanna', 'Frida']


def test_crew_here_datum_wird_auf_heute_geklemmt():
    """Beliebige Zukunftstage waren eine Fern-Vorschau auf fremde
    Aufenthaltsorte — der Endpoint beantwortet nur noch heute ±1."""
    my_days = _days(('2026-07-28', 'FRA'), ('2026-07-29', 'BKK'),
                    ('2026-07-30', 'BKK'))
    with patch.object(A, '_crew_ops_today', return_value=TODAY), \
         patch.object(A, '_crew_roster_days', return_value=my_days), \
         patch.object(A, '_profile_load', return_value={'profile': LH_PROFILE}), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_crew_hotel_at', return_value=None), \
         patch.object(A, '_crew_here_members', return_value=[]) as mock_members, \
         A.app.test_request_context(
             f'/api/user/crew-here/{VIEWER}?iata=BKK&date=2026-09-15'):
        data = A.get_crew_here(VIEWER).get_json()
    assert data['date'] == TODAY
    assert mock_members.call_args[0][1] == TODAY


# ── Sichtbarkeits-Gates der Scan-Stufe ──────────────────────────────────────

def _scan(tokens, profs, iatas, visibility=None, days=None):
    visibility = visibility or {}
    days = days or {}
    with patch.object(A, '_crew_push_registered_tokens', return_value=tokens), \
         patch.object(A, '_profiles_load_bulk', return_value=profs), \
         patch.object(A, '_user_current_iata',
                      side_effect=lambda t: iatas.get(t)), \
         patch.object(A, '_layover_visibility_get',
                      side_effect=lambda t: visibility.get(t, True)), \
         patch.object(A, '_crew_roster_days',
                      side_effect=lambda t: days.get(t, _days(('2026-07-29', 'BKK')))), \
         patch.object(A, '_crew_hotel_at', return_value=None):
        return A._crew_here_members_uncached('BKK', TODAY)


def test_scan_respektiert_share_roster_und_share_location():
    tokens = ['AT-OK', 'AT-NOROSTER', 'AT-NOLOC']
    profs = {'AT-OK': {}, 'AT-NOROSTER': {'share_roster': False},
             'AT-NOLOC': {'share_location': False}}
    iatas = dict.fromkeys(tokens, 'BKK')
    assert [m['token'] for m in _scan(tokens, profs, iatas)] == ['AT-OK']


def test_scan_respektiert_layover_visibility_optout():
    tokens = ['AT-OK', 'AT-HIDDEN']
    profs = {'AT-OK': {}, 'AT-HIDDEN': {}}
    iatas = dict.fromkeys(tokens, 'BKK')
    out = _scan(tokens, profs, iatas, visibility={'AT-HIDDEN': False})
    assert [m['token'] for m in out] == ['AT-OK']


def test_scan_schliesst_family_konten_aus():
    tokens = ['AT-CREW', 'AT-FAM']
    profs = {'AT-CREW': {}, 'AT-FAM': {'account_type': 'family'}}
    iatas = dict.fromkeys(tokens, 'BKK')
    assert [m['token'] for m in _scan(tokens, profs, iatas)] == ['AT-CREW']


def test_scan_nimmt_nur_die_richtige_station():
    tokens = ['AT-BKK', 'AT-FRA']
    profs = {'AT-BKK': {}, 'AT-FRA': {}}
    iatas = {'AT-BKK': 'BKK', 'AT-FRA': 'FRA'}
    assert [m['token'] for m in _scan(tokens, profs, iatas)] == ['AT-BKK']


# ── Stale-while-revalidate (Owner-Vorfall 2026-08-02 abends) ────────────────
#
# Der Members-Sweep dauert auf Prod real 142–273 s (Roster-Read pro Token).
# Er darf deshalb NIE im Request-Pfad laufen: frisch → Treffer, abgelaufen →
# alter Stand sofort + EIN Hintergrund-Rebuild, kalt → None (Endpoint:
# reason='warming'). Diese Tests fixieren genau diesen Vertrag.

class TestCrewHereStaleWhileRevalidate:

    def _clear(self):
        with A._CREW_HERE_LOCK:
            A._CREW_HERE_MEMO.clear()
            A._CREW_HERE_REBUILDING.clear()

    def _wait_rebuilt(self, key, timeout=3.0):
        import time as _t
        end = _t.time() + timeout
        while _t.time() < end:
            with A._CREW_HERE_LOCK:
                if key in A._CREW_HERE_MEMO and key not in A._CREW_HERE_REBUILDING:
                    return True
            _t.sleep(0.02)
        return False

    def test_kalt_gibt_none_und_baut_im_hintergrund(self):
        self._clear()
        members = [{'token': 'AT-X', 'profile': {}, 'ops': {'here': True}}]
        with patch.object(A, '_crew_here_members_uncached',
                          return_value=members) as mock_sweep:
            assert A._crew_here_members('FRA', TODAY) is None
            assert self._wait_rebuilt(('FRA', TODAY)), 'Rebuild kam nie an'
            assert A._crew_here_members('FRA', TODAY) == members
            assert mock_sweep.call_count == 1, 'zweiter Aufruf muss Memo-Treffer sein'
        self._clear()

    def test_abgelaufen_liefert_stale_sofort_und_erneuert(self):
        import time as _t
        self._clear()
        alt = [{'token': 'AT-ALT', 'profile': {}, 'ops': {'here': True}}]
        neu = [{'token': 'AT-NEU', 'profile': {}, 'ops': {'here': True}}]
        key = ('FRA', TODAY)
        with A._CREW_HERE_LOCK:
            A._CREW_HERE_MEMO[key] = (alt, _t.time() - A._CREW_HERE_TTL - 1)
        with patch.object(A, '_crew_here_members_uncached', return_value=neu):
            # Der abgelaufene Stand kommt SOFORT zurück — kein Blockieren.
            assert A._crew_here_members('FRA', TODAY) == alt
            assert self._wait_rebuilt(key), 'Hintergrund-Rebuild kam nie an'
            assert A._crew_here_members('FRA', TODAY) == neu
        self._clear()

    def test_single_flight_nur_ein_rebuild_pro_key(self):
        import threading as _th
        self._clear()
        gate = _th.Event()
        calls = []

        def _langsam(st, ref):
            calls.append(st)
            gate.wait(2.0)
            return []

        with patch.object(A, '_crew_here_members_uncached', side_effect=_langsam):
            assert A._crew_here_members('FRA', TODAY) is None
            assert A._crew_here_members('FRA', TODAY) is None   # kein 2. Sweep
            gate.set()
            self._wait_rebuilt(('FRA', TODAY))
            assert len(calls) == 1, f'Single-flight verletzt: {len(calls)} Sweeps'
        self._clear()

    def test_endpoint_meldet_warming_statt_erfundener_null(self):
        # Aufbau wie `_crew_here` oben — nur liefert der Members-Layer None
        # („wissen wir noch nicht", Sweep läuft): der Endpoint muss das als
        # reason='warming' durchreichen statt eine 0 zu behaupten.
        with patch.object(A, '_crew_ops_today', return_value=TODAY), \
             patch.object(A, '_crew_roster_days', return_value=_days(
                 ('2026-07-29', 'BKK'))), \
             patch.object(A, '_profile_load', return_value={'profile': LH_PROFILE}), \
             patch.object(A, '_friends_load', return_value={'friends': []}), \
             patch.object(A, '_crew_hotel_at', return_value=None), \
             patch.object(A, '_crew_here_members', return_value=None), \
             A.app.test_request_context(f'/api/user/crew-here/{VIEWER}?iata=BKK'):
            body = A.get_crew_here(VIEWER).get_json()
        assert body.get('ok') is True
        assert body.get('reason') == 'warming'
        assert body.get('counts', {}).get('total') == 0
        assert body.get('crew') == []
