"""Crew-Prefetch-Diät (2026-07-29) — der Monats-Vorabruf riss das Tagesbudget.

LAGE (gemessen, ax_api_budget): common_crewlist 28.07. = 92 → 29.07. = 2.876
Calls/Tag, seit der Prefetch-Horizont auf 31 Tage/48 Legs ging. Am 29.07.
12:50 UTC stand lhfoD bei 5.303 ≥ 5.000 ⇒ ALLE Hintergrund-Calls aller User
wurden übersprungen.

Diese Datei sichert die vier Gegenmaßnahmen ab:
  1. HORIZONT 3 Kalendertage (heute + 2) statt 31.
  2. HARTES LEG-BUDGET pro Lauf (8), zeitlich nächste zuerst, SICHTBAR gekappt.
  3. LEER-MARKER: eine leere LH-Antwort erzeugt keine Cache-Zeile — ohne
     Marker würde dasselbe Leg bei jedem Kick neu geholt.
  4. DECKEL-VERTEIDIGUNG: Nutzer-Taps (Check-in, Hotel) laufen interaktiv,
     der server-seitige Re-Import in app.py als Hintergrund.
"""
import os
import sys
import threading
import time

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend  # noqa: E402  (Blueprint-Registrierung)
from blueprints import lh_flightops as fo  # noqa: E402

_L = 'https://api.lufthansa.com/v1/flightops'


def _leg_event(flight, date, access='A1'):
    return {'eventType': 'FLIGHT', '_links': {'crewList': {'href':
            _L + f'/COMMON_CREWLIST?flightDesignator={flight}'
            f'&flightDate={date}Z&departureAirport=FRA'
            f'&arrivalAirport=MUC&accessCode={access}'}}}


def _resp(events):
    return {'rosterDays': [{'day': 'x', 'events': events}]}


# ── 1. HORIZONT ─────────────────────────────────────────────────────────────

def test_horizont_deckt_heute_bis_uebermorgen():
    """Heute + 2 Tage wird vorgewärmt, Tag 3 nicht mehr. Begründung im Banner:
    ferne Legs sind bis zum Flug oft veraltet UND teilen praktisch keinen
    zweiten AeroX-User (gemessen 29.07.: 84 Cache-Zeilen = 84 distinct Flüge
    für heute)."""
    evs = [_leg_event('LH100', '2026-07-29'), _leg_event('LH101', '2026-07-31'),
           _leg_event('LH102', '2026-08-01')]
    legs = fo._crew_prefetch_legs(_resp(evs), today='2026-07-29')
    assert [l['flight'] for l in legs] == ['LH100', 'LH101']
    assert fo._CREW_PREFETCH_DAYS == 3


# ── 2. HARTES LEG-BUDGET ────────────────────────────────────────────────────

def test_deckel_nimmt_die_zeitlich_naechsten_legs_und_loggt(caplog):
    """Vielflieger/Kurzstrecke: der Deckel greift, nimmt die FRÜHESTEN Legs
    und sagt es im Log (stilles Abschneiden war die Ursache des Vorfalls)."""
    evs = ([_leg_event('LH%03d' % i, '2026-07-29') for i in range(6)]
           + [_leg_event('LH%03d' % i, '2026-07-31') for i in range(100, 106)])
    with caplog.at_level('INFO'):
        legs = fo._crew_prefetch_legs(_resp(evs), today='2026-07-29')
    assert len(legs) == fo._CREW_PREFETCH_MAX_LEGS == 8
    # Zuerst der heutige Dienst — genau das tippt der User an.
    assert [l['date'] for l in legs[:6]] == ['2026-07-29'] * 6
    assert 'gedeckelt' in caplog.text
    # Ohne Überlauf wird NICHT geloggt (kein Log-Rauschen im Normalfall).
    caplog.clear()
    with caplog.at_level('INFO'):
        fo._crew_prefetch_legs(_resp(evs[:3]), today='2026-07-29')
    assert 'gedeckelt' not in caplog.text


# ── 3. LEER-MARKER ──────────────────────────────────────────────────────────

def _prefetch_env(monkeypatch, empty=True):
    monkeypatch.setattr(fo, '_rot_hour_used', lambda: 0)
    monkeypatch.setattr(fo, '_lhfo_day_used', lambda: 0)
    monkeypatch.setattr(fo, '_CREW_PREFETCH_SLEEP_S', 0.0)
    monkeypatch.setattr(fo, '_crew_cache_scan', lambda f, d: [])
    monkeypatch.setattr(fo, '_crew_cache_put', lambda *a, **k: None)
    monkeypatch.setattr(fo, 'parse_crew_list',
                        lambda resp: [] if empty else [{'name': 'CREW'}])
    fo._refresh_all_state['drain'] = False
    fo._crew_prefetch_empty.clear()
    calls = []
    monkeypatch.setattr(fo, 'crew_list',
                        lambda tok, f, d, dep, arr, ac, interactive=False:
                        calls.append((f, d, interactive)) or {'ok': 1})
    return calls


def test_leere_liste_wird_nicht_im_stundentakt_nachgebohrt(monkeypatch):
    """LH füllt die Crew-Liste erst kurz vor Abflug. Eine leere Antwort
    erzeugt KEINE Cache-Zeile (_crew_cache_put steigt bei [] aus) — ohne
    Marker holte der nächste Kick dasselbe leere Leg erneut."""
    calls = _prefetch_env(monkeypatch, empty=True)
    legs = [{'flight': 'LH400', 'date': '2026-07-31', 'dep': 'FRA',
             'arr': 'JFK', 'access': 'A1'}]
    assert fo._crew_prefetch_run('AT-U', legs)['skipped'] == 1
    assert len(calls) == 1
    assert fo._crew_prefetch_run('AT-U', legs) == {'prefetched': 0,
                                                   'skipped': 1, 'failed': 0}
    assert len(calls) == 1, 'zweiter Lauf darf LH nicht nochmal fragen'
    # Nach Ablauf der TTL wird wieder nachgesehen (die Liste füllt sich ja).
    fo._crew_prefetch_empty[('LH400', '2026-07-31')] = (
        time.time() - fo._CREW_PREFETCH_EMPTY_TTL_S - 1)
    fo._crew_prefetch_run('AT-U', legs)
    assert len(calls) == 2


def test_leer_marker_gilt_nur_fuer_den_prefetch(monkeypatch):
    """Der Marker ist eine HINTERGRUND-Sparmaßnahme. Ein Nutzer-Tap
    (flightops_crewlist) darf davon nie ausgebremst werden."""
    fo._crew_prefetch_empty.clear()
    fo._crew_prefetch_empty_mark('LH400', '2026-07-31')
    assert fo._crew_prefetch_empty_recent('LH400', '2026-07-31') is True
    import inspect
    src = inspect.getsource(fo.flightops_crewlist)
    assert '_crew_prefetch_empty' not in src


# ── 4. DECKEL-VERTEIDIGUNG: Taps interaktiv, Hintergrund im Hintergrund ─────

def _capture_api_get(monkeypatch, ret):
    seen = []

    # Signatur spiegelt die ECHTE (inkl. `priority`/`status_out` seit der
    # dritten Budget-Stufe 09.08.) — ein veralteter Mock wäre ein Test-Infra-
    # Bug und würde hier als Produkt-Fehler missverstanden.
    def _fake(tok, path, params=None, interactive=False, status_out=None,
              priority=False):
        seen.append((path, interactive))
        return ret
    monkeypatch.setattr(fo, '_api_get', _fake)
    monkeypatch.setattr(fo, '_access_state', lambda t: ('ok', 'AT-ACCESS'))
    return seen


def test_checkin_tap_laeuft_interaktiv(monkeypatch):
    """Vorfall 29.07.: nach dem Reißen des Tages-Deckels starben ALLE
    Hintergrund-Calls — der Check-in-Tap lief unter dem Hintergrund-Deckel und
    gab dem User 502. Nutzer-Aktionen gehören in den reservierten Headroom."""
    seen = _capture_api_get(monkeypatch, {'x': 1})
    monkeypatch.setattr(fo, '_resolve_link_params',
                        lambda *a, **k: {'flightDesignator': 'LH400'})
    monkeypatch.setattr(fo, 'parse_check_in_times', lambda r: {'briefing': 'x'})
    r = backend.app.test_client().post('/api/lh/flightops/checkin/testtok-fo',
                                       json={'flight': 'LH400',
                                             'date': '2026-07-29'})
    assert r.status_code == 200
    assert seen == [('/COMMON_CHECK_IN_TIMES', True)]


def test_checkin_ohne_link_cache_ebenfalls_interaktiv(monkeypatch):
    """Auch der Doku-Default-Pfad (kein Link im Cache — nach jedem Deploy der
    Normalfall) muss interaktiv sein. `interactive` darf dabei NICHT als
    Query-Param bei LH landen (check_in_times hat **extra)."""
    seen = []

    def _fake(tok, path, params=None, interactive=False, status_out=None,
              priority=False):
        seen.append((path, interactive, dict(params or {})))
        return {'x': 1}
    monkeypatch.setattr(fo, '_api_get', _fake)
    monkeypatch.setattr(fo, '_access_state', lambda t: ('ok', 'AT-ACCESS'))
    monkeypatch.setattr(fo, '_resolve_link_params', lambda *a, **k: None)
    monkeypatch.setattr(fo, 'parse_check_in_times', lambda r: {'briefing': 'x'})
    backend.app.test_client().post('/api/lh/flightops/checkin/testtok-fo',
                                   json={'flight': 'LH400',
                                         'date': '2026-07-29'})
    assert seen and seen[0][1] is True
    assert 'interactive' not in seen[0][2]


def test_hotel_tap_laeuft_interaktiv(monkeypatch):
    seen = _capture_api_get(monkeypatch, {'hotels': []})
    monkeypatch.setattr(fo, 'parse_crew_hotel', lambda r: [])
    r = backend.app.test_client().post('/api/lh/flightops/hotel/testtok-fo',
                                       json={'station': 'JFK'})
    assert r.status_code == 200
    assert seen == [('/COMMON_CREW_HOTEL_INFO', True)]


def test_briefing_bleibt_hintergrund(monkeypatch):
    """Gegenprobe: der Cron-Pfad (daily_briefing) darf den interaktiven
    Headroom NICHT anfassen — sonst schützt die Reserve nichts."""
    seen = _capture_api_get(monkeypatch, {'x': 1})
    fo.crew_hotel('AT-U', 'JFK')
    fo.crew_list('AT-U', 'LH400', '2026-07-29', 'FRA', 'JFK', 'A1')
    assert [s[1] for s in seen] == [False, False]


def test_server_reimport_ist_hintergrundarbeit(monkeypatch):
    """`_maybe_refresh_flightops` ist fire-and-forget (die Antwort an den User
    kommt aus den gespeicherten Briefings). Er lief mit leerem Body und galt
    damit als INTERAKTIV — er fraß die Reserve echter Taps."""
    done = threading.Event()
    seen = {}

    def _fake_import(token):
        from flask import request
        seen['body'] = request.get_json(silent=True) or {}
        done.set()
        return ('', 200)
    monkeypatch.setattr(fo, 'flightops_import', _fake_import)
    monkeypatch.setattr(backend, '_flightops_active', lambda t: True)
    backend._flightops_refresh_last.pop('AT-U', None)
    backend._maybe_refresh_flightops('AT-U')
    assert done.wait(5), 'Refresh-Thread lief nicht'
    assert seen['body'].get('background')
