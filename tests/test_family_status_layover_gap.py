"""Family-Status: leg-loser Layover-Tag darf NICHT „Basis <Homebase>" werden.

BUG (Prod, Tibor 2026-08-10): Die Family-Karte zeigte „Basis Frankfurt /
Zuhause", während Tibor mitten in einer ICN-Rotation stand. Sein Tages-Row in
`user_ical_briefings` sah so aus:

    ical_summary = 'LH 712: FRA-ICN (Tag 2/2) · X · Layover [ICN] (Tag 1/3)'
    ical_sectors = KEINE            (der Cross-Date-Leg hängt am Vortag,
                                     Landung 00:33Z am Folgetag)
    ical_start / ical_end = leer

URSACHE: `_load_crew_status_for_family` hat eine eigene, alte Ad-hoc-Ableitung
(`_parse_roster_day` + Fall-Kette). Für so einen Tag greift KEIN Zweig:
`is_flight` ist wegen der „LH 712: FRA-ICN"-Prosa True, es gibt aber weder ein
Dienst-Fenster noch `en_eff`/`landed_obs` → weder `roster_layover` noch
`roster_today_home` wird gesetzt. `resolve_crew_live_state` bekam dann
`layover_iata=None` und fiel im leg-losen Zweig auf STATE_HOME „Basis
Frankfurt" zurück.

Der KALENDER-Pfad (`_load_crew_roster_days`) war längst auf den geteilten
Helfer `app._feed_nightstop_ort` umgestellt — die STATUS-Karte nie. Genau
deshalb las sich der Fix wie „wurde angeblich gefixt, sieht noch genauso aus".

Abgedeckt:
  (a) Tibor-Fall: leg-loser Tag mit „Layover [ICN]" → state=layover/ICN
  (b) echter Homebase-Tag ohne Dienst → bleibt home (kein Layover erfunden,
      der positive Home-Beweis wird NICHT überschrieben)
  (c) leg-loser Tag OHNE jeden Layover-Beleg → wird NICHT zum Layover geraten
  (d) Sektoren schlagen den Summary-Beleg (der geteilte Helfer bleibt Chef)
  (e) Summary-Regex pur

ALLE Daten sind RELATIV zu `FW._fw_today()` (Roster-Zone Europe/Berlin) —
keine fixen Kalenderdaten, der Test bleibt zeitstabil.
"""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import datetime as dt                                     # noqa: E402
import sys                                                # noqa: E402
import types                                              # noqa: E402

import pytest                                             # noqa: E402

import app as A                                           # noqa: E402
import blueprints.family_watch as FW                      # noqa: E402
from blueprints.crew_live_state import (                  # noqa: E402
    STATE_HOME, STATE_LAYOVER)

GRANTS = {'next_flight', 'layover_place'}


# ── SB-Stub (Muster: tests/aerox/test_family_current_leg.py) ─────────────────

class _Q:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, _name):
        return lambda *a, **k: self

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _SB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _Q(self._rows)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    # sys.modules['app']-Pin gegen die test_calculation-Reimport-Kontamination
    # (gleiches Muster wie test_crew_live_state / test_family_current_leg).
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    FW._LIVE_FIX_MEMO.clear()
    yield
    FW._LIVE_FIX_MEMO.clear()
    if prev is not None:
        sys.modules['app'] = prev


def _today():
    return FW._fw_today()


def _env(monkeypatch, rows, homebase='FRA'):
    monkeypatch.setattr(FW, '_get_sb', lambda: (True, _SB(rows)))
    monkeypatch.setattr(FW, '_load_crew_profile',
                        lambda t: {'homebase': homebase})
    monkeypatch.setattr(A, '_profile_load', lambda t: {}, raising=False)
    # Kein Roster-Snapshot → keine Sektoren aus dem Steuer-Pfad, keine
    # Flugnummern für Board-Beobachtungen (exakt die Prod-Lage des Bugs).
    monkeypatch.setattr(A, '_roster_snapshot_read', lambda t: {},
                        raising=False)
    monkeypatch.setattr(A, '_flight_obs_merged',
                        lambda *a, **k: None, raising=False)


# ── (e) Summary-Regex pur ───────────────────────────────────────────────────

def test_summary_layover_iata_liest_das_myTime_muster():
    assert FW._summary_layover_iata(
        'LH 712: FRA-ICN (Tag 2/2) · X · Layover [ICN] (Tag 1/3)') == 'ICN'
    assert FW._summary_layover_iata('Layover [EWR]') == 'EWR'
    assert FW._summary_layover_iata('layover [pek] … 10:55 LT Pickup') == 'PEK'
    # Kein Beleg → None (nichts erfinden).
    assert FW._summary_layover_iata('LH 712: FRA-ICN (Tag 2/2)') is None
    assert FW._summary_layover_iata('Off Day (OF)') is None
    assert FW._summary_layover_iata(None) is None


# ── (a) DER Bug: Tibor mitten in der ICN-Rotation ───────────────────────────

def test_legloser_layover_tag_ist_layover_nicht_basis(monkeypatch):
    rows = [{
        'datum': _today().isoformat(),
        'ical_summary': ('LH 712: FRA-ICN (Tag 2/2) · X · '
                         'Layover [ICN] (Tag 1/3)'),
        'ical_location': '',
        'ical_start': None,
        'ical_end': None,
        'raw_event': {},          # KEINE ical_sectors (Cross-Date-Leg am Vortag)
    }]
    _env(monkeypatch, rows)

    st = FW._load_crew_status_for_family('tok-tibor-icn-gap', GRANTS)

    cs = st.get('crew_state')
    assert cs is not None, 'crew_state fehlt im Family-Status'
    assert cs['state'] == STATE_LAYOVER, (
        f"leg-loser Layover-Tag wurde {cs['state']} statt layover "
        f"(Titel: {cs['text']['title']!r})")
    assert 'Basis' not in (cs['text']['title'] or '')
    assert st['layover_place'] == 'ICN'
    assert st['layover_place_city']          # Städtename aufgelöst
    assert st['home_now'] is False


# ── (b) Echter Homebase-Tag bleibt home ─────────────────────────────────────

def test_echter_homebase_tag_bleibt_home(monkeypatch):
    rows = [{
        'datum': _today().isoformat(),
        'ical_summary': 'Off Day (OF)',
        'ical_location': 'FRA',
        'ical_start': None,
        'ical_end': None,
        'raw_event': {},
    }]
    _env(monkeypatch, rows)

    st = FW._load_crew_status_for_family('tok-home-day', GRANTS)

    cs = st.get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_HOME
    # Der neue Fallback darf einen POSITIVEN Home-Beweis nie überschreiben.
    assert st['layover_place'] is None
    assert st['home_now'] is True


# ── (c) Kein Beleg → kein geratener Layover ─────────────────────────────────

def test_legloser_tag_ohne_beleg_raet_keinen_layover(monkeypatch):
    rows = [{
        'datum': _today().isoformat(),
        'ical_summary': 'LH 712: FRA-ICN (Tag 2/2)',   # KEIN Layover-Marker
        'ical_location': '',
        'ical_start': None,
        'ical_end': None,
        'raw_event': {},
    }]
    _env(monkeypatch, rows)

    st = FW._load_crew_status_for_family('tok-no-evidence', GRANTS)

    cs = st.get('crew_state')
    assert cs is not None
    assert cs['state'] == STATE_HOME, 'ohne Beleg darf nichts geraten werden'
    assert st['layover_place'] is None


# ── (d) Sektoren schlagen den Summary-Beleg ────────────────────────────────

def test_sektoren_schlagen_den_summary_beleg(monkeypatch):
    """Trägt der Tag ECHTE Sektoren, entscheidet der geteilte Helfer aus
    ihnen — der Summary-Beleg ist nur die Fallback-Spalte (keine
    Doppel-Logik). Hier landet der letzte Sektor an der Homebase → home."""
    today = _today()
    dep = dt.datetime.combine(today, dt.time(6, 0), dt.timezone.utc)
    arr = dep + dt.timedelta(hours=2)

    def _z(d):
        return d.strftime('%Y-%m-%dT%H:%M:%SZ')

    rows = [{
        'datum': today.isoformat(),
        # Widersprüchlicher Alt-Beleg im Summary — die Sektoren gewinnen.
        'ical_summary': 'LH 713: ICN-FRA · Layover [ICN] (Tag 3/3)',
        'ical_location': '',
        'ical_start': None,
        'ical_end': None,
        'raw_event': {'ical_sectors': [
            {'flight': 'LH713', 'from': 'ICN', 'to': 'FRA',
             'dep_iso': _z(dep), 'arr_iso': _z(arr)},
        ]},
    }]
    _env(monkeypatch, rows)

    st = FW._load_crew_status_for_family('tok-sectors-win', GRANTS)

    assert st['layover_place'] is None, (
        'Ankunft an der Homebase ist kein Layover')
