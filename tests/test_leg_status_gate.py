"""Leg-Status Plausibilitäts-/Physik-Gate (blueprints/leg_status_gate.py).

Deckt den FlightState-Härtungs-Fall (a) LH454→SFO ab: ein Board, das für einen
11-h-Langstreckenflug fälschlich früh „gelandet 13:03" meldet, darf diesen
terminalen Status NICHT durchreichen — die früheste physikalisch mögliche
Ankunft ist Stunden später. Nicht-terminale Status und plausible Landungen
laufen unverändert durch; fehlen die Belege → fail-open.

Reine Funktion, kein I/O, keine app-Imports — vollständig deterministisch.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone

import pytest

from blueprints.leg_status_gate import (
    is_terminal_landed, earliest_possible_arrival_ts, landed_status_plausible,
    gated_leg_status,
)


# Geo-Fixpunkte (grob) für die Distanz-Rechnung.
FRA = (50.03, 8.57)
SFO = (37.62, -122.38)     # ~9150 km great-circle von FRA
MUC = (48.35, 11.78)       # ~300 km von FRA (Kurzstrecke)


def _ts(iso):
    dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


# ── is_terminal_landed ──────────────────────────────────────────────────────
@pytest.mark.parametrize('status,expected', [
    ('Gelandet 13:03', True),
    ('Landed', True),
    ('Arrived', True),
    ('at gate', True),
    ('on blocks', True),
    ('angekommen', True),
    ('baggage', True),
    ('Airborne', False),
    ('Departed', False),
    ('Boarding', False),
    ('Delayed', False),
    ('Scheduled', False),
    ('', False),
    (None, False),
])
def test_is_terminal_landed(status, expected):
    assert is_terminal_landed(status) is expected


# ── earliest_possible_arrival_ts ────────────────────────────────────────────
def test_earliest_arrival_longhaul_hours_later():
    dep = _ts('2026-07-13T10:00:00Z')
    got = earliest_possible_arrival_ts(dep, FRA, SFO)
    assert got is not None
    hrs = (got - dep) / 3600.0
    # FRA→SFO ~9150 km / 950 km/h ≈ 9.6 h + Overhead → deutlich > 9 h.
    assert hrs > 9.0


def test_earliest_arrival_shorthaul_under_an_hour():
    dep = _ts('2026-07-13T10:00:00Z')
    got = earliest_possible_arrival_ts(dep, FRA, MUC)
    hrs = (got - dep) / 3600.0
    assert hrs < 1.0            # FRA→MUC ~300 km → weit unter 1 h


def test_earliest_arrival_none_without_dep_or_coords():
    assert earliest_possible_arrival_ts(None, FRA, SFO) is None
    assert earliest_possible_arrival_ts(_ts('2026-07-13T10:00:00Z'), None, SFO) is None
    assert earliest_possible_arrival_ts(_ts('2026-07-13T10:00:00Z'), FRA, None) is None


# ── landed_status_plausible — DER KERNFALL (a) ──────────────────────────────
def test_bogus_early_landing_longhaul_rejected():
    # LH454 FRA→SFO: Abflug 10:00Z, Board meldet „gelandet 13:03" um 13:05Z —
    # physikalisch unmöglich (frühestens ~19:36Z). → NICHT plausibel.
    dep = _ts('2026-07-13T10:00:00Z')
    now = _ts('2026-07-13T13:05:00Z')
    assert landed_status_plausible(
        'Gelandet 13:03', now=now, dep_ts=dep, dep_ll=FRA, arr_ll=SFO,
        sched_arr_iso='2026-07-13T20:30:00Z') is False


def test_real_landing_after_arrival_accepted():
    # Derselbe Flug, aber „now" liegt real nach der frühesten Ankunft → plausibel.
    dep = _ts('2026-07-13T10:00:00Z')
    now = _ts('2026-07-13T20:35:00Z')
    assert landed_status_plausible(
        'Gelandet 20:31', now=now, dep_ts=dep, dep_ll=FRA, arr_ll=SFO,
        sched_arr_iso='2026-07-13T20:30:00Z') is True


def test_shorthaul_landing_plausible_after_short_flight():
    # FRA→MUC: Abflug 10:00Z, „gelandet" um 11:05Z → physikalisch OK (~<1 h Flug).
    dep = _ts('2026-07-13T10:00:00Z')
    now = _ts('2026-07-13T11:05:00Z')
    assert landed_status_plausible(
        'Landed', now=now, dep_ts=dep, dep_ll=FRA, arr_ll=MUC,
        sched_arr_iso='2026-07-13T11:00:00Z') is True


def test_landing_within_slack_before_sched_accepted():
    # 10 min vor Fahrplan-Ankunft „gelandet" (Board läuft leicht vor) → toleriert
    # (Slack 15 min). Kurzstrecke, dep passt.
    dep = _ts('2026-07-13T10:00:00Z')
    now = _ts('2026-07-13T10:50:00Z')
    assert landed_status_plausible(
        'Landed', now=now, dep_ts=dep, dep_ll=FRA, arr_ll=MUC,
        sched_arr_iso='2026-07-13T11:00:00Z') is True


def test_non_terminal_status_never_gated():
    # airborne/delayed: nie terminal → immer plausibel, egal wie früh.
    now = _ts('2026-07-13T10:05:00Z')
    dep = _ts('2026-07-13T10:00:00Z')
    for st in ('Airborne', 'Departed', 'Delayed', 'Boarding', 'Scheduled'):
        assert landed_status_plausible(
            st, now=now, dep_ts=dep, dep_ll=FRA, arr_ll=SFO,
            sched_arr_iso='2026-07-13T20:30:00Z') is True


def test_fail_open_without_any_bounds():
    # Kein sched_arr, kein dep_ts, keine Koordinaten → nichts beweisbar →
    # fail-open (Rohstatus bleibt).
    assert landed_status_plausible(
        'Gelandet 13:03', now=_ts('2026-07-13T13:05:00Z')) is True


def test_sched_arr_only_gate_without_coords():
    # Nur Fahrplan-Ankunft bekannt (keine Koordinaten): „gelandet" 6 h vor
    # sched_arr − Slack → unplausibel.
    now = _ts('2026-07-13T14:00:00Z')
    assert landed_status_plausible(
        'Landed', now=now, sched_arr_iso='2026-07-13T20:30:00Z') is False
    # nach sched_arr → plausibel.
    assert landed_status_plausible(
        'Landed', now=_ts('2026-07-13T20:31:00Z'),
        sched_arr_iso='2026-07-13T20:30:00Z') is True


def test_phys_only_gate_without_sched():
    # Nur dep_ts + Koordinaten (kein Fahrplan): physikalische Schranke greift.
    dep = _ts('2026-07-13T10:00:00Z')
    assert landed_status_plausible(
        'Landed', now=_ts('2026-07-13T13:00:00Z'), dep_ts=dep,
        dep_ll=FRA, arr_ll=SFO) is False
    assert landed_status_plausible(
        'Landed', now=_ts('2026-07-13T20:00:00Z'), dep_ts=dep,
        dep_ll=FRA, arr_ll=SFO) is True


def test_est_arr_preferred_over_sched_arr():
    # Ist-Ankunft (est_arr) verschiebt die Schranke nach hinten (Verspätung):
    # sched 20:30, est 22:00 → um 20:45 „gelandet" ist vor est−Slack → unplausibel.
    now = _ts('2026-07-13T20:45:00Z')
    assert landed_status_plausible(
        'Landed', now=now, sched_arr_iso='2026-07-13T20:30:00Z',
        est_arr_iso='2026-07-13T22:00:00Z') is False


# ── gated_leg_status ────────────────────────────────────────────────────────
def test_gated_status_suppresses_bogus_landing():
    dep = _ts('2026-07-13T10:00:00Z')
    now = _ts('2026-07-13T13:05:00Z')
    assert gated_leg_status(
        'Gelandet 13:03', now=now, dep_ts=dep, dep_ll=FRA, arr_ll=SFO,
        sched_arr_iso='2026-07-13T20:30:00Z') is None


def test_gated_status_passthrough_plausible():
    dep = _ts('2026-07-13T10:00:00Z')
    now = _ts('2026-07-13T20:35:00Z')
    assert gated_leg_status(
        'Gelandet 20:31', now=now, dep_ts=dep, dep_ll=FRA, arr_ll=SFO,
        sched_arr_iso='2026-07-13T20:30:00Z') == 'Gelandet 20:31'


def test_gated_status_passthrough_non_terminal():
    assert gated_leg_status('Airborne', now=_ts('2026-07-13T10:05:00Z'),
                            dep_ts=_ts('2026-07-13T10:00:00Z'),
                            dep_ll=FRA, arr_ll=SFO) == 'Airborne'


def test_gate_never_raises_on_garbage():
    # Robustheit: unparsebare Zeiten / kaputte Koordinaten → fail-open, kein Wurf.
    assert gated_leg_status('Landed', now=None, sched_arr_iso='not-a-date',
                            dep_ll=('x', 'y'), arr_ll=None) == 'Landed'
