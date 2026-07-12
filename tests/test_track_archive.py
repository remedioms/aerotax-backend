"""Unit-Tests für die PUREN Verdichtungs-Funktionen der Track-Permanenz
(blueprints/track_archive.py — Permanenz-Plan (c), docs/data-permanence-plan.md).

Getestet werden nur die DB-freien Bausteine:
  • douglas_peucker_indices / simplify_track  (Polyline-Verdichtung)
  • split_legs                                 (Gap-/Boden-Gruppierung)
  • compact_leg                                (Leg → Archiv-Row)

Invarianten:
  DP-1  Endpunkte bleiben IMMER erhalten, Reihenfolge bleibt stabil.
  DP-2  Eine Gerade kollabiert auf 2 Punkte; ein echter Knick überlebt.
  DP-3  simplify_track garantiert die Obergrenze max_points — auch für
        pathologische Zickzack-Tracks (harter Fallback).
  GAP-1 Zeitlücke > 45 min splittet in zwei Legs (wie _flown_track_db).
  GAP-2 on_ground=true beendet das Leg; der Boden-Punkt gehört zu KEINEM Leg
        (kein Geister-Flieger im Archiv).
  LEG-1 compact_leg rundet lat/lon auf 4 Dezimalen, setzt service_date aus dem
        ersten Punkt und fällt ohne Flugnummer auf 'DEP-ARR' als PK-Key zurück.
"""
import math

from blueprints.track_archive import (
    GAP_SEC, MIN_LEG_POINTS,
    douglas_peucker_indices, simplify_track, split_legs, compact_leg,
)

T0 = 1_783_600_000  # feste Uhr (2026-07-09 ~08:26Z)


def _row(ts, lat, lon, on_ground=False, **kw):
    r = {'ts': ts, 'lat': lat, 'lon': lon, 'on_ground': on_ground}
    r.update(kw)
    return r


# ── Douglas-Peucker ──────────────────────────────────────────────────────────

def test_dp_keeps_endpoints_and_order():
    pts = [(50.0, 8.0), (50.1, 8.2), (50.2, 8.1), (50.3, 8.4), (50.4, 8.3)]
    idx = douglas_peucker_indices(pts, eps=0.0001)
    assert idx[0] == 0 and idx[-1] == len(pts) - 1
    assert idx == sorted(idx)


def test_dp_straight_line_collapses_to_two_points():
    pts = [(50.0 + i * 0.01, 8.0 + i * 0.01) for i in range(50)]
    idx = douglas_peucker_indices(pts, eps=0.001)
    assert idx == [0, 49]


def test_dp_corner_survives():
    # Hinflug nach Osten, dann scharfer Knick nach Norden — der Knickpunkt
    # (Index 10) MUSS überleben, sonst wird aus dem Winkel eine Diagonale.
    pts = ([(50.0, 8.0 + i * 0.1) for i in range(11)]
           + [(50.0 + i * 0.1, 9.0) for i in range(1, 11)])
    idx = douglas_peucker_indices(pts, eps=0.01)
    assert 10 in idx
    assert idx[0] == 0 and idx[-1] == len(pts) - 1
    assert len(idx) < len(pts)


def test_dp_short_inputs_unchanged():
    assert douglas_peucker_indices([], 0.01) == []
    assert douglas_peucker_indices([(1.0, 2.0)], 0.01) == [0]
    assert douglas_peucker_indices([(1.0, 2.0), (3.0, 4.0)], 0.01) == [0, 1]


def test_simplify_respects_max_points_on_realistic_arc():
    # Gebogener Track (Großkreis-artig) mit 800 Punkten → ≤80, Enden erhalten.
    pts = [(50.0 + 5.0 * math.sin(i / 800.0 * math.pi),
            8.0 + i * 0.05) for i in range(800)]
    idx = simplify_track(pts, max_points=80)
    assert 2 <= len(idx) <= 80
    assert idx[0] == 0 and idx[-1] == 799
    assert idx == sorted(idx)


def test_simplify_hard_fallback_on_pathological_zigzag():
    # Jeder Punkt ist ein maximaler Ausreißer — DP allein verdichtet das kaum;
    # der uniforme Fallback MUSS die Obergrenze trotzdem garantieren.
    pts = [(50.0 + (1.0 if i % 2 else -1.0), 8.0 + i * 0.001)
           for i in range(500)]
    idx = simplify_track(pts, max_points=80)
    assert len(idx) <= 80
    assert idx[0] == 0 and idx[-1] == 499


def test_simplify_small_track_untouched():
    pts = [(50.0 + i * 0.1, 8.0) for i in range(10)]
    assert simplify_track(pts, max_points=80) == list(range(10))


# ── Gap-/Boden-Gruppierung ───────────────────────────────────────────────────

def test_split_no_gap_single_leg():
    rows = [_row(T0 + i * 120, 50.0, 8.0 + i * 0.01) for i in range(20)]
    legs = split_legs(rows)
    assert len(legs) == 1 and len(legs[0]) == 20


def test_split_on_time_gap():
    # 46-min-Lücke zwischen Punkt 9 und 10 → genau zwei Legs (wie Tier 1).
    rows = ([_row(T0 + i * 120, 50.0, 8.0 + i * 0.01) for i in range(10)]
            + [_row(T0 + 9 * 120 + GAP_SEC + 60 + i * 120, 51.0, 9.0 + i * 0.01)
               for i in range(10)])
    legs = split_legs(rows)
    assert len(legs) == 2
    assert len(legs[0]) == 10 and len(legs[1]) == 10


def test_split_exact_gap_boundary_does_not_split():
    # Lücke == GAP_SEC (nicht >) bleibt EIN Leg — Grenzfall wie _flown_track_db.
    rows = [_row(T0, 50.0, 8.0), _row(T0 + GAP_SEC, 50.1, 8.1)]
    assert len(split_legs(rows)) == 1


def test_split_on_ground_ends_leg_and_is_excluded():
    rows = ([_row(T0 + i * 120, 50.0, 8.0 + i * 0.01) for i in range(6)]
            + [_row(T0 + 6 * 120, 50.0, 8.06, on_ground=True)]
            + [_row(T0 + (7 + i) * 120, 50.1, 8.1 + i * 0.01) for i in range(6)])
    legs = split_legs(rows)
    assert len(legs) == 2
    assert all(not r['on_ground'] for leg in legs for r in leg)
    assert len(legs[0]) == 6 and len(legs[1]) == 6


def test_split_leading_ground_rows_ignored():
    rows = ([_row(T0 + i * 60, 50.0, 8.0, on_ground=True) for i in range(3)]
            + [_row(T0 + 300 + i * 120, 50.0, 8.0 + i * 0.01) for i in range(8)])
    legs = split_legs(rows)
    assert len(legs) == 1 and len(legs[0]) == 8


def test_split_empty_and_all_ground():
    assert split_legs([]) == []
    assert split_legs([_row(T0, 50.0, 8.0, on_ground=True)]) == []


# ── compact_leg ──────────────────────────────────────────────────────────────

def _leg_rows(n=30, flight='LH1558', origin='FRA', dest='LIS'):
    return [_row(T0 + i * 120, 50.123456 + i * 0.01, 8.654321 + i * 0.02,
                 flight=flight, origin=origin, dest=dest,
                 alt_ft=30000.0, gs_kt=440.7) for i in range(n)]


def test_compact_leg_row_shape_and_rounding():
    row = compact_leg('DAINV', _leg_rows())
    assert row is not None
    assert row['reg'] == 'DAINV'
    assert row['flight'] == 'LH1558'
    assert row['dep'] == 'FRA' and row['arr'] == 'LIS'
    assert row['service_date'] == '2026-07-09'      # UTC-Tag des ersten Punkts
    assert row['pt_count'] == len(row['points']) >= 2
    ts, lat, lon, alt, gs = row['points'][0]
    assert ts == T0
    assert lat == round(50.123456, 4) and lon == round(8.654321, 4)
    assert alt == 30000 and gs == 441                # ints, kein float-Müll
    # Endpunkte der Verdichtung = Endpunkte des Legs:
    assert row['points'][-1][0] == T0 + 29 * 120


def test_compact_leg_respects_max_points():
    row = compact_leg('DAIXS', _leg_rows(n=500), max_points=80)
    assert row is not None and row['pt_count'] <= 80


def test_compact_leg_flight_fallback_citypair():
    rows = _leg_rows(flight=None)
    row = compact_leg('DAIXS', rows)
    assert row['flight'] == 'FRA-LIS'                # PK-Fallback statt ''
    # Audit B11: komplett route-lose Legs kriegen '@<HH>' (UTC-Stunde des
    # ersten Punkts) statt '' — sonst kollidierten ALLE route-losen Legs
    # derselben Reg am selben Tag im PK und nur der letzte überlebte.
    row2 = compact_leg('DAIXS', _leg_rows(flight=None, origin=None, dest=None))
    import time as _t
    assert row2['flight'] == '@%s' % _t.strftime('%H', _t.gmtime(T0))


def test_compact_leg_too_short_returns_none():
    assert compact_leg('DAINV', _leg_rows(n=MIN_LEG_POINTS - 1)) is None
    assert compact_leg('DAINV', []) is None


def test_compact_leg_drops_rows_without_fix_or_ts():
    rows = _leg_rows(n=10)
    rows[3]['lat'] = None
    rows[5]['ts'] = None
    row = compact_leg('DAINV', rows)
    assert row is not None
    kept_ts = [p[0] for p in row['points']]
    assert T0 + 3 * 120 not in kept_ts               # lat-loser Punkt raus
