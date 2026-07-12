"""Regressions-Sweep 2026-07-12 #12 — zweistufiges Budget in
observe_adsb_breadcrumbs.

Fund: Commit 04482af kalibrierte max_process=900 für 2 Hub-Punkte und legte
die Hub-Zeilen VOR die Sweep-Zeilen; Commit 8375998 verdreifachte die Hubs
auf 6 ohne Cap-Anpassung → tagsüber (~400-600 Hub-Zeilen + >600 Sweep-
Zeilen) verhungerte der Sweep-Anteil (Anflug-/Taxi-Crumbs an Nicht-Hub-EU-
Airports, Kern von Unified-Track C1) — dieselbe Fehlerklasse, die 04482af
fixte, nur eine Ebene höher.

Fix unter Test: priority_rows (Hubs) mit EIGENEM Cap, rows (Sweep) mit
GARANTIERTEM eigenen max_process-Budget; Dedup (seen) bleibt geteilt.

KEIN echter Netz-/DB-Zugriff: _sb/_nearest_airport gepatcht.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from unittest.mock import patch

import blueprints.aerox_data_blueprint as BP


class _CapSB:
    """Fängt den aircraft_track-Upsert ab."""

    def __init__(self):
        self.rows = []

    def table(self, name):
        return self

    def upsert(self, rows, **kw):
        self.rows = rows
        return self

    def execute(self):
        return None


def _row(reg, airborne=True):
    return {'reg': reg, 'lat': 50.0, 'lon': 8.5,
            'alt': 10000 if airborne else 0,
            'speed': 400 if airborne else 12,
            'heading': 90, 'flight': 'LH1', 'on_ground': not airborne}


def _run(sweep, hubs):
    cap = _CapSB()
    with patch.object(BP, '_sb', return_value=cap), \
            patch.object(BP, '_nearest_airport', return_value=('FRA', 1.0)):
        n = BP.observe_adsb_breadcrumbs(sweep, priority_rows=hubs)
    return n, cap.rows


def test_sweep_budget_garantiert_trotz_hub_flut():
    """DER Fund: 950 Hub-Zeilen (6 Hubs, Tages-Peak) + 100 Sweep-Zeilen.
    Vor dem Fix (ein 900er-Gesamt-Cap, Hubs zuerst): NULL Sweep-Crumbs.
    Jetzt: Hubs am eigenen Cap gedeckelt, der Sweep bekommt sein volles
    Budget — alle 100 Sweep-Regs werden geschrieben."""
    hubs = [_row(f'HB{i:04d}') for i in range(950)]
    sweep = [_row(f'SW{i:04d}') for i in range(100)]
    n, rows = _run(sweep, hubs)
    written = {r['reg'] for r in rows}
    sweep_written = {r for r in written if r.startswith('SW')}
    hub_written = {r for r in written if r.startswith('HB')}
    assert sweep_written == {f'SW{i:04d}' for i in range(100)}, \
        'Sweep-Zeilen dürfen nie von Hub-Volumen verhungert werden'
    # Hubs am EIGENEN Cap gedeckelt (Schreibvolumen bleibt begrenzt).
    assert len(hub_written) == 600
    assert n == len(rows)


def test_dedup_zwischen_hub_und_sweep_bleibt():
    """Ein Flieger nahe einem Hub taucht in Hub- UND Sweep-Zeilen auf —
    wird trotz getrennter Budgets nur EINMAL geschrieben."""
    hubs = [_row('DAIMC'), _row('HB0001')]
    sweep = [_row('DAIMC'), _row('SW0001')]
    n, rows = _run(sweep, hubs)
    regs = [r['reg'] for r in rows]
    assert regs.count('DAIMC') == 1
    assert set(regs) == {'DAIMC', 'HB0001', 'SW0001'}
    assert n == 3


def test_geparkte_flieger_kosten_kein_budget():
    """Park-Spam (on_ground, gs<=3) wird weiter VOR dem Budget verworfen."""
    hubs = [{'reg': f'PK{i:03d}', 'lat': 50.0, 'lon': 8.5, 'alt': 0,
             'speed': 0, 'heading': 0, 'on_ground': True} for i in range(50)]
    hubs.append(_row('HB0001'))
    n, rows = _run([_row('SW0001')], hubs)
    assert {r['reg'] for r in rows} == {'HB0001', 'SW0001'}
    assert n == 2


def test_alte_signatur_ohne_priority_rows_unveraendert():
    """Rückwärtskompatibel: Aufruf nur mit rows verhält sich wie bisher."""
    cap = _CapSB()
    with patch.object(BP, '_sb', return_value=cap), \
            patch.object(BP, '_nearest_airport', return_value=('FRA', 1.0)):
        n = BP.observe_adsb_breadcrumbs([_row('SW0001'), _row('SW0002')])
    assert n == 2
    assert {r['reg'] for r in cap.rows} == {'SW0001', 'SW0002'}
