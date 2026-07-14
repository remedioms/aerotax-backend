"""GOLDEN-/CONTRACT-TEST friends-today ↔ iOS (Owner 2026-07-12).

Warum es diesen Test gibt: Heute brachen DREI Format-Dinge in der
friends-today-Antwort (crew_state-Leg-Keys, Rang-Cache, Rollen-Feld) jeweils
erst AUF DEM GERÄT — beide Seiten grün, Feature kaputt. Das bewährte Muster
dagegen ist der crew_state-Wire-Contract (tests/test_crew_state_contract.py ↔
iOS CrewStateContractTests.swift). Dieser Test hebt das Muster auf die
KOMPLETTE friends-today-Antwort: ein reiches, komplett gemocktes Szenario
läuft durch den ECHTEN Endpoint `get_friends_today` (app.py), die Antwort ist
als Golden-JSON gepinnt in
    tests/fixtures/friends_today_golden.json
und BYTE-GLEICH als Fixture im iOS-Test hinterlegt
    (AeroTaxTests/FriendsTodayGoldenTests.swift — decodiert es mit
    APIClient.FriendsTodayResp/FriendTodayEntry/CrewState).
Ändert eine Seite den Vertrag, bricht ihr Test — nicht mehr das Feature.

Regel: Wer hier etwas ändert, MUSS die iOS-Fixture identisch mitziehen
(und umgekehrt). Fixture-Änderung nur SYNCHRON auf beiden Seiten.

Das Szenario (Frozen-Clock 2026-07-09T10:00:00Z, Berliner Tag 2026-07-09):
  • Kai   — FLIEGT (FRA→JFK, airborne-Board-Obs, ECHTE aircraft_live-Position
            mit track/gs, pre_phase=null, flights_live[0].live gesetzt).
  • Pia   — PRE_FLIGHT-Timeline am OUTSTATION (BCN→FRA, +15 dep-Delay,
            iCal-„Pickup 1145" → pre_phase 'crewbus'/„Im Crewbus").
  • Ole   — FREI (duty=free → „Heute frei") + MORGEN-Frühflug binnen 24 h
            → crew_state_next (Vorabend-Bordkarte).
  • Mia   — LAYOVER (legloser Tag, reader_facts.layover_ort=SFO).

Deterministik: die Uhr ist via datetime-Subclass eingefroren (Modul-Attribut
im echten `datetime`-Modul + app-Binding gepatcht — auch die
`import datetime as _cs_dt`-Inline-Imports des Endpoints sehen sie); alle
Daten-Reads (_friends_load/_profiles_load_bulk/_roster_snapshot_read/
_flight_obs_merged/_aircraft_live_pos/…) sind injizierte Fakes. KEIN echter
Netz-/DB-Zugriff.

DOKUMENTIERTER BEFUND (nicht fixen, nur wissen): friends-today trägt KEINE
Rang-/Rollen-Felder (position/airline) pro Freund — Kai hat sie im gemockten
Profil, die Antwort lässt sie weg (iOS bezieht Rang aus /api/user/friends).
Kommen sie dazu, MUSS dieses Golden + die iOS-Fixture nachgezogen werden.
"""
import datetime as _dt_mod
import hashlib
import json
import os
import sys
from unittest.mock import patch

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import pytest

import app as A

# ── Frozen-Clock ─────────────────────────────────────────────────────────────
# Poor-man's freezegun (freezegun ist nicht in requirements): eine
# datetime-Subclass mit fixem now()/utcnow(). Gepatcht wird (a) das Attribut
# im ECHTEN datetime-Modul (fängt alle `import datetime`/-Inline-Imports, z. B.
# `import datetime as _cs_dt` im Endpoint und die lokalen Imports in
# _airport_local_now/_board_local_to_utc_iso) und (b) das from-Import-Binding
# `A.datetime`. Arithmetik/astimezone erhalten die Subclass (Py ≥ 3.8).

_REAL_DATETIME = _dt_mod.datetime


class _FrozenDatetime(_REAL_DATETIME):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return FROZEN_NOW.replace(tzinfo=None)
        return FROZEN_NOW.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return FROZEN_NOW.replace(tzinfo=None)


FROZEN_NOW = _FrozenDatetime(2026, 7, 9, 10, 0, 0, tzinfo=_dt_mod.timezone.utc)
DATUM = '2026-07-09'          # = Berliner Betriebstag der Frozen-Clock

GOLDEN_PATH = os.path.join(os.path.dirname(__file__), 'fixtures',
                           'friends_today_golden.json')

# ── Szenario-Daten (alles fix, nichts zeit-relativ) ──────────────────────────

KAI = 'AT-GOLDEN-KAI-FLYER-01'    # fliegt gerade FRA→JFK
PIA = 'AT-GOLDEN-PIA-PREFLT-02'   # pre_flight Outstation BCN (Crewbus-Phase)
OLE = 'AT-GOLDEN-OLE-FREE-0003'   # heute frei + Morgen-Frühflug (crew_state_next)
MIA = 'AT-GOLDEN-MIA-LAYOVER-4'   # Layover-Ruhetag SFO

FRIENDS = [KAI, PIA, OLE, MIA]

PROFILES = {
    # position/airline BEWUSST gesetzt: dokumentiert, dass friends-today sie
    # aktuell NICHT ausliefert (siehe Kopf-Kommentar — Rang kommt aus /friends).
    KAI: {'name': 'Kai Winter', 'homebase': 'FRA', 'share_roster': True,
          'share_location': True, 'avatar_url': '/api/avatar/kai.png',
          'position': 'CPT', 'airline': 'LH'},
    PIA: {'name': 'Pia Sommer', 'homebase': 'FRA', 'share_roster': True,
          'share_location': True, 'commute_minutes': 45},
    OLE: {'name': 'Ole Nord', 'homebase': 'HAM', 'share_roster': True,
          'share_location': True},
    MIA: {'name': 'Mia Berg', 'homebase': 'MUC', 'share_roster': True,
          'share_location': True},
}

DAYS = {
    KAI: [{'datum': DATUM, 'klass': 'Z72', 'marker': 'FLUG',
           'routing': 'FRA-JFK',
           'reader_facts': {'layover_ort': 'JFK',
                            'flight_numbers': ['LH400'],
                            'start_time': '06:55', 'end_time': '17:05'},
           'ical_sectors': [{'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
                             'dep_iso': '2026-07-09T08:00:00Z',
                             'arr_iso': '2026-07-09T16:30:00Z'}]}],
    PIA: [{'datum': DATUM, 'klass': 'Z72', 'marker': 'FLUG',
           'routing': 'BCN-FRA',
           'ical_summary': 'Pickup 1145 Hotel Arts',
           'reader_facts': {'flight_numbers': ['LH1137'],
                            'start_time': '11:45', 'end_time': '14:35'},
           'ical_sectors': [{'flight': 'LH1137', 'from': 'BCN', 'to': 'FRA',
                             'dep_iso': '2026-07-09T12:10:00Z',
                             'arr_iso': '2026-07-09T14:05:00Z'}]}],
    OLE: [{'datum': DATUM, 'klass': 'FREI', 'marker': 'OFF DAY',
           'reader_facts': {}, 'ical_sectors': []},
          # Morgen-Frühflug binnen 24 h → crew_state_next (Vorabend-Bordkarte).
          {'datum': '2026-07-10', 'klass': 'Z72', 'marker': 'FLUG',
           'routing': 'HAM-MUC', 'reader_facts': {},
           'ical_sectors': [{'flight': 'LH2071', 'from': 'HAM', 'to': 'MUC',
                             'dep_iso': '2026-07-10T06:30:00Z',
                             'arr_iso': '2026-07-10T07:45:00Z'}]}],
    MIA: [{'datum': DATUM, 'klass': 'Z73', 'marker': 'LAYOVER SFO',
           'reader_facts': {'layover_ort': 'SFO'}, 'ical_sectors': []}],
}

SNAPSHOTS = {fr: {'taken_at': '2026-07-09T05:00:00Z', 'tage': DAYS[fr]}
             for fr in FRIENDS}

# Board-Obs (Shape wie app._flight_obs_merged): sched/esti in STATIONS-Ortszeit
# (naive ISO) — der Endpoint wandelt sie via _board_local_to_utc_iso nach UTC.
OBS = {
    # Kai airborne: FRA 10:00 CEST = 08:00Z, JFK 12:30 EDT = 16:30Z.
    'LH400': {'status': 'airborne', 'cancelled': False,
              'dep_delay_min': 12, 'arr_delay_min': 9,
              'delay_min': 9, 'delay_side': 'arr', 'delay_known': True,
              'reg': 'DAIXK',
              'sched_dep': '2026-07-09T10:00:00',
              'esti_dep': '2026-07-09T10:12:00',
              'sched_arr': '2026-07-09T12:30:00',
              'esti_arr': '2026-07-09T12:39:00',
              'sides': ['dep', 'arr']},
    # Pia grounded/delayed: BCN 14:10 CEST = 12:10Z, FRA 16:05 CEST = 14:05Z.
    'LH1137': {'status': 'delayed', 'cancelled': False,
               'dep_delay_min': 15, 'arr_delay_min': None,
               'delay_min': None, 'delay_side': 'dep', 'delay_known': True,
               'reg': 'DAIMD',
               'sched_dep': '2026-07-09T14:10:00',
               'esti_dep': '2026-07-09T14:25:00',
               'sched_arr': '2026-07-09T16:05:00',
               'esti_arr': None,
               'sides': ['dep']},
}

# ECHTE aircraft_live-Position NUR für Kais Maschine (über dem Atlantik).
LIVE_POS_LH400 = {'lat': 50.03, 'lon': -20.5, 'track': 285.0, 'gs': 462.0,
                  'alt': 36000, 'on_ground': False,
                  'seen_ts': '2026-07-09T09:58:20Z', 'source': 'aircraft_live'}


def _fake_obs_merged(flight_no, date=None, dep_iata=None, arr_iata=None,
                     live=True, free_only=False):
    """Spiegelt die ECHTE _flight_obs_merged-Signatur (CLAUDE.md-Regel)."""
    return OBS.get(str(flight_no or '').upper())


def _fake_aircraft_live_pos(*args, **kwargs):
    """Spiegelt _aircraft_live_pos(flight=…, dep=…) → (pos, route, reg, type)."""
    flight = kwargs.get('flight') or (args[0] if args else None)
    if str(flight or '').upper() == 'LH400':
        return (dict(LIVE_POS_LH400), None, 'D-AIXK', 'A343')
    return (None, None, None, None)


@pytest.fixture(autouse=True)
def _pin_app_module():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    A._FRIENDS_TODAY_MEMO.clear()
    yield
    A._FRIENDS_TODAY_MEMO.clear()
    if prev is not None:
        sys.modules['app'] = prev


def _call_endpoint(token='AT-GOLDEN-VIEWER-000', raw_response=False):
    """Der ECHTE Endpoint mit komplett injizierten Daten + Frozen-Clock."""
    with patch.object(_dt_mod, 'datetime', _FrozenDatetime), \
         patch.object(A, 'datetime', _FrozenDatetime), \
         patch.dict(os.environ, {'FLIGHTSTATE_SHADOW': '',
                                 'FLIGHTSTATE_LIVE_FRIENDS': ''}), \
         patch.object(A, '_friends_load', return_value={'friends': FRIENDS}), \
         patch.object(A, '_profiles_load_bulk', return_value=PROFILES), \
         patch.object(A, '_maybe_refresh_calendar_feed'), \
         patch.object(A, '_roster_snapshot_read',
                      side_effect=lambda fr: SNAPSHOTS.get(fr)), \
         patch.object(A, '_friend_briefing_day_sectors',
                      return_value=(None, None, None, None)), \
         patch.object(A, '_flight_obs_merged', side_effect=_fake_obs_merged), \
         patch('blueprints.aerox_data_blueprint._aircraft_live_pos',
               side_effect=_fake_aircraft_live_pos), \
         patch.dict(A._store, {}, clear=True):
        A._FRIENDS_TODAY_MEMO.clear()
        with A.app.test_request_context(
                f'/api/user/friends-today/{token}?datum={DATUM}'):
            resp = A.get_friends_today(token)
    return resp if raw_response else resp.get_json()


def _golden():
    with open(GOLDEN_PATH, encoding='utf-8') as f:
        return json.load(f)


# ── Tests ────────────────────────────────────────────────────────────────────

def test_friends_today_matches_golden_fixture_completely():
    """DER Kern: die KOMPLETTE Antwort (alle Keys + Werte, rekursiv) muss dem
    gepinnten Golden entsprechen. Bricht bei JEDER Format-/Wert-Änderung —
    dann BEIDE Fixtures (hier + FriendsTodayGoldenTests.swift) synchron
    nachziehen, nie nur eine Seite."""
    got = _call_endpoint()
    want = _golden()
    assert got == want, (
        'friends-today weicht vom Golden ab. Diff-Hilfe:\n'
        + json.dumps(got, indent=2, ensure_ascii=False, sort_keys=True))


def test_friends_today_is_deterministic_across_runs():
    """Zwei unabhängige Läufe (Memo geleert) müssen IDENTISCH sein — ein
    Unterschied wäre ein Nicht-Determinismus-Fund (Owner will das gemeldet
    bekommen, nicht weggemittelt)."""
    a = _call_endpoint()
    b = _call_endpoint()
    assert a == b, 'friends-today ist nicht deterministisch (Fund melden!)'


def test_friends_today_disables_http_cache():
    """Live-/personalisierter Crew-State hat ausschließlich den kontrollierten
    90-s-Servermemo; CF/URLSession dürfen keinen zweiten Stale-Layer bilden."""
    resp = _call_endpoint(raw_response=True)
    cc = resp.headers.get('Cache-Control', '')
    assert 'private' in cc and 'no-store' in cc and 'max-age=0' in cc


def test_golden_fixture_semantics_pinned():
    """Semantische Anker AUS DEM FIXTURE (nicht aus dem Live-Lauf) — schützt
    gegen versehentliche Edits an der eingecheckten Datei selbst (gleiche Idee
    wie test_wire_contract_json_is_valid_and_stable im crew_state-Contract)."""
    g = _golden()
    assert set(g) == {'datum', 'count', 'friends_today'}
    assert g['datum'] == DATUM and g['count'] == 4
    by_name = {e['name']: e for e in g['friends_today']}
    assert set(by_name) == {'Kai Winter', 'Pia Sommer', 'Ole Nord', 'Mia Berg'}

    kai = by_name['Kai Winter']
    cs = kai['crew_state']
    assert cs['state'] == 'flying'
    # Leg-Richtung + Wire-Keys des Server-Legs (dep/arr/flight_no/dep_iso…).
    leg = cs['current_leg']
    assert (leg['dep'], leg['arr'], leg['flight_no']) == ('FRA', 'JFK', 'LH400')
    assert leg['est_dep_iso'] == '2026-07-09T08:12:00Z'
    assert leg['est_arr_iso'] == '2026-07-09T16:39:00Z'
    assert leg['reg'] == 'D-AIXK'          # _fmt_reg: 'DAIXK' → 'D-AIXK'
    # Glyph-Kurs: position MUSS track/gs tragen (Tuning-Runde 2026-07-12).
    assert cs['position']['track'] == 285.0
    assert cs['position']['gs'] == 462.0
    assert cs['pre_phase'] is None
    # flights_live: Board-Zeiten EINMAL nach UTC gewandelt + echte Live-Pos.
    fl = kai['flights_live'][0]
    assert fl['sched_dep_iso'] == '2026-07-09T08:00:00Z'
    assert fl['sched_arr_iso'] == '2026-07-09T16:30:00Z'
    assert fl['live']['lat'] == 50.03 and fl['live']['on_ground'] is False

    pia = by_name['Pia Sommer']
    assert pia['crew_state']['state'] == 'pre_flight'
    assert pia['crew_state']['pre_phase'] == 'crewbus'
    assert pia['crew_state']['pre_phase_label'] == 'Im Crewbus'
    assert pia['crew_state']['current_leg']['delay_min'] == 15

    ole = by_name['Ole Nord']
    assert ole['crew_state']['state'] == 'home'
    assert ole['crew_state']['text']['title'] == 'Heute frei'
    nxt = ole['crew_state_next']
    assert nxt['state'] == 'pre_flight'
    assert nxt['current_leg']['flight_no'] == 'LH2071'
    assert nxt['current_leg']['dep_iso'] == '2026-07-10T06:30:00Z'

    mia = by_name['Mia Berg']
    assert mia['crew_state']['state'] == 'layover'
    assert mia['crew_state']['text']['title'] == 'Layover San Francisco'
    assert mia['crew_state_next'] is None

    # DOKUMENTIERTER BEFUND (Kopf-Kommentar): KEINE Rang-/Rollen-Felder in der
    # Antwort, obwohl Kais Profil position/airline trägt. Taucht eins auf,
    # ist das eine Vertrags-Änderung → BEIDE Fixtures nachziehen.
    for e in g['friends_today']:
        assert 'position' not in e and 'airline' not in e and 'rank' not in e
