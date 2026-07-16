"""Freunde-Roster: Ist-Zeiten für VERGANGENE Tour-Tage (Owner/Fable 2026-07-15).

Symptom: Das Freunde-Kalender-Sheet zeigte für Vortage nur nacktes „Gelandet"/
„Abgeflogen" statt der Ist-Zeiten — obwohl die Esti in airport_delay_obs
persistiert sind. Drei Fixes in `_enrich_leg_delays`:

  FIX 1  Vergangenheits-Fenster nur für den persistenten Read öffnen
         (`past_horizon_h`, Default 30 = bisheriges Verhalten; der Friend-Roster
         ruft mit weitem Horizont, alle anderen Aufrufer unverändert). Der Live-
         Board-Scan bleibt via `_flight_obs_merged` auf heute begrenzt.
  FIX 2  Für Vergangenheits-Legs ohne Ankunfts-Esti aus dem Merge fällt der
         Enricher auf `_flight_facts_from_obs` (persistente Blueprint-Quelle,
         Station-Offset-ISO) zurück und füllt NUR die Lücken.
  FIX 3  Stale „Abgeflogen": dep-Status airborne + keine Ankunfts-Obs +
         Plan-Ankunft > 6 h vor jetzt → ehrlich 'landed' (keine erfundene Zeit).

KEIN echter Netz-/DB-Zugriff — `_flight_obs_merged` und `_flight_facts_from_obs`
sind gemockt. Spiegelt die Muster aus tests/test_leg_delay_enrichment.py.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

import app as A
import blueprints.aerox_data_blueprint as ADB


@pytest.fixture(autouse=True)
def _clear_caches():
    A._FLIGHT_MERGE_CACHE.clear()
    A._AX_CODESHARE_CACHE['ts'] = 0.0
    A._AX_CODESHARE_CACHE['map'] = {}
    yield
    A._FLIGHT_MERGE_CACHE.clear()


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _sector(flight='LH893', frm='RIX', to='FRA', dep_iso=None, arr_iso=None):
    s = {'flight': flight, 'from': frm, 'to': to}
    if dep_iso is not None:
        s['dep_iso'] = dep_iso
    if arr_iso is not None:
        s['arr_iso'] = arr_iso
    return s


def _merged(delay_min=None, delay_known=False, delay_side=None,
            dep_delay_min=None, arr_delay_min=None, status=None,
            cancelled=False, esti_dep=None, esti_arr=None, reg=None,
            sides=None, sched_dep=None, sched_arr=None):
    return {
        'ok': True, 'delay_min': delay_min, 'delay_known': delay_known,
        'delay_side': delay_side, 'dep_delay_min': dep_delay_min,
        'arr_delay_min': arr_delay_min, 'status': status,
        'cancelled': cancelled, 'esti_dep': esti_dep, 'esti_arr': esti_arr,
        'reg': reg, 'sides': sides or {'dep': None, 'arr': None},
        'sched_dep': sched_dep, 'sched_arr': sched_arr,
    }


# ══════════════════════════════════════════════════════════════════════════════
# FIX 1 — Vergangenheits-Fenster (past_horizon_h)
# ══════════════════════════════════════════════════════════════════════════════
def test_far_past_leg_enriched_with_wide_horizon():
    # Leg 40 h alt → mit past_horizon_h = 24*35 angereichert (SB-Read erlaubt).
    dep = _now() - timedelta(hours=40)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=8,
                                           delay_side='arr', arr_delay_min=8,
                                           status='landed')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['delay_known'] is True
    assert secs[0]['delay_min'] == 8


def test_far_past_leg_skipped_with_default_horizon():
    # Derselbe 40-h-Leg wird mit dem Default-Horizont (30) NICHT gescannt.
    dep = _now() - timedelta(hours=40)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('should not scan with default '
                                                 'horizon')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'))
    assert 'delay_known' not in secs[0]


def test_wide_horizon_does_not_break_future_guard():
    # Weiter Vergangenheits-Horizont hebt die Zukunfts-Grenze (+27 h) NICHT auf.
    dep = _now() + timedelta(hours=30)
    secs = [_sector(dep_iso=_iso(dep))]
    with patch.object(A, '_flight_obs_merged',
                      side_effect=AssertionError('future must stay guarded')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert 'delay_known' not in secs[0]


# ══════════════════════════════════════════════════════════════════════════════
# FIX 2 — Persistente Blueprint-Quelle füllt Ist-Zeit-Lücken
# ══════════════════════════════════════════════════════════════════════════════
def test_facts_fallback_fills_est_arr_when_merge_has_none(monkeypatch):
    # Merge liefert Status (landed) ohne Ankunfts-Esti; _flight_facts_from_obs
    # liefert est_arr (Station-Offset-ISO) → Sektor bekommt est_arr_iso.
    dep = _now() - timedelta(hours=40)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA', dep_iso=_iso(dep))]
    facts = {'est_arr': '2026-07-15T08:15:00+02:00',
             'arr_status': 'Gelandet', 'arr_delay_min': 5, 'reg': 'D-AIRX'}
    monkeypatch.setattr(ADB, '_flight_facts_from_obs',
                        lambda *a, **k: facts)
    monkeypatch.setattr(A, '_tail_recently_active', lambda r: True)
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False, status='landed',
                                           esti_arr=None)):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['est_arr_iso'] == '2026-07-15T08:15:00+02:00'
    # Delay-Lücke ebenfalls aus den Facts nachgezogen (ehrlich delay_known=True).
    assert secs[0]['delay_known'] is True
    assert secs[0]['delay_min'] == 5
    assert secs[0]['reg'] == 'D-AIRX'


def test_facts_fallback_builds_leg_when_merge_none(monkeypatch):
    # Kein Merge-Signal (m None), aber persistente Facts für den Vortag → der Leg
    # wird trotzdem angereichert (statt Legacy-Skip).
    dep = _now() - timedelta(hours=40)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA', dep_iso=_iso(dep))]
    facts = {'est_arr': '2026-07-15T08:15:00+02:00', 'arr_status': 'Gelandet',
             'arr_delay_min': 0}
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: facts)
    with patch.object(A, '_flight_obs_merged', return_value=None):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['est_arr_iso'] == '2026-07-15T08:15:00+02:00'
    assert secs[0]['delay_known'] is True


def test_facts_fallback_no_signal_stays_legacy(monkeypatch):
    # m None UND Facts leer → Legacy-Form (keine neuen Keys, nichts erfunden).
    dep = _now() - timedelta(hours=40)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA', dep_iso=_iso(dep))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged', return_value=None):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert 'delay_known' not in secs[0]
    assert 'est_arr_iso' not in secs[0]


def test_facts_fallback_never_overwrites_merge_esti(monkeypatch):
    # Hat der Merge bereits ein Ankunfts-Esti, wird _flight_facts_from_obs gar
    # nicht befragt (Vorrang der m-Werte).
    dep = _now() - timedelta(hours=40)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA', dep_iso=_iso(dep))]
    monkeypatch.setattr(
        ADB, '_flight_facts_from_obs',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('facts must not be consulted when merge has esti')))
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=0,
                                           delay_side='arr',
                                           esti_arr='2026-07-15T09:00:00Z')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    # UTC-Board-Format → durch _board_local_to_utc_iso (bleibt UTC).
    assert secs[0]['est_arr_iso'] == '2026-07-15T09:00:00Z'


def test_facts_fallback_not_used_for_present_leg(monkeypatch):
    # Für ein HEUTIGES/laufendes Leg (dep in der Zukunft) wird die Facts-Quelle
    # NICHT befragt — der Fallback ist rein für die Vergangenheit.
    dep = _now() + timedelta(hours=1)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA', dep_iso=_iso(dep))]
    monkeypatch.setattr(
        ADB, '_flight_facts_from_obs',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('no facts fallback for present/future legs')))
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=0,
                                           delay_side='dep', status='boarding')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] == 'boarding'


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3 — Stale „Abgeflogen" ehrlich als 'landed'
# ══════════════════════════════════════════════════════════════════════════════
def test_stale_departed_becomes_landed(monkeypatch):
    # dep-Status „Abgeflogen", keine Ankunfts-Obs, Plan-Ankunft 36 h vor jetzt
    # → Status ehrlich 'landed'. Keine erfundene Ist-Zeit.
    dep = _now() - timedelta(hours=38)
    arr = _now() - timedelta(hours=36)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False,
                                           status='Abgeflogen')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] == 'landed'
    # keine erfundene Ist-Ankunft.
    assert secs[0].get('est_arr_iso') is None


def test_stale_departed_kept_when_arrival_recent(monkeypatch):
    # Gegenprobe: Plan-Ankunft erst 1 h vorbei → der „Abgeflogen"-Status bleibt
    # (der Flug kann noch am Rollen/gerade gelandet sein — nicht überschreiben).
    dep = _now() - timedelta(hours=2)
    arr = _now() - timedelta(hours=1)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False,
                                           status='Abgeflogen')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] == 'Abgeflogen'


def test_stale_departed_not_applied_when_arrival_observed(monkeypatch):
    # „Abgeflogen" + Plan-Ankunft 36 h vorbei, ABER es GIBT eine Ankunfts-Obs
    # (arr_delay_min) → FIX 3 greift nicht (echte Ankunfts-Beobachtung schlägt
    # die Stale-Heuristik; der Roh-/gemessene Status läuft normal weiter).
    dep = _now() - timedelta(hours=38)
    arr = _now() - timedelta(hours=36)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=True, delay_min=12,
                                           delay_side='arr', arr_delay_min=12,
                                           status='Abgeflogen')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    # Ankunft beobachtet → FIX 3 rührt den Status nicht auf 'landed'.
    assert secs[0]['status'] == 'Abgeflogen'
    assert secs[0]['arr_delay_min'] == 12


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3b — LH890 FRA→RIX: kein sched_arr vom Board, Plan-Ankunft nur aus dem Leg
# (arr_iso) bzw. — wenn auch die fehlt — aus dep + Großkreis/v_max. airborne→landed.
# ══════════════════════════════════════════════════════════════════════════════
def test_stale_airborne_landed_from_leg_arr_iso_no_board_sched(monkeypatch):
    # RIX wird nie geharvestet → weder Merge noch Facts liefern sched_arr; die
    # Plan-Ankunft kommt allein aus der Leg-eigenen arr_iso. airborne + überfällig
    # (> 30 h) → 'landed'.
    dep = _now() - timedelta(hours=33)
    arr = _now() - timedelta(hours=31)
    secs = [_sector(flight='LH890', frm='FRA', to='RIX',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False, status='airborne',
                                           sched_arr=None)):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] == 'landed'
    assert secs[0].get('est_arr_iso') is None


def test_stale_airborne_landed_from_block_time_when_no_arr_anchor(monkeypatch):
    # Kein arr_iso, kein board/facts sched_arr → Plan-Ankunft-Proxy = dep +
    # Großkreis/v_max (früheste physikalisch mögliche Ankunft). 33 h alt → längst
    # gelandet, obwohl KEIN einziger Ankunfts-Soll-Anker existiert.
    dep = _now() - timedelta(hours=33)
    secs = [_sector(flight='LH890', frm='FRA', to='RIX', dep_iso=_iso(dep))]
    secs[0].pop('arr_iso', None)
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False,
                                           status='Abgeflogen')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] == 'landed'


def test_stale_airborne_cancelled_stays(monkeypatch):
    # Ein STORNIERTER Flug ist NIE „gelandet" — die Stale-Regel darf cancelled nicht
    # überschreiben.
    dep = _now() - timedelta(hours=33)
    arr = _now() - timedelta(hours=31)
    secs = [_sector(flight='LH890', frm='FRA', to='RIX',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False, status='airborne',
                                           cancelled=True)):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] != 'landed'
    assert secs[0]['cancelled'] is True


# ══════════════════════════════════════════════════════════════════════════════
# FIX 3c — „Geplant"-Leiche: Vergangenheits-Leg mit vorhandener est_arr, Status im
# scheduled-Bucket ('Geplant'/None) + Plan-Ankunft lange vorbei → 'landed'.
# ══════════════════════════════════════════════════════════════════════════════
def test_geplant_corpse_with_est_arr_becomes_landed(monkeypatch):
    dep = _now() - timedelta(hours=15)
    arr = _now() - timedelta(hours=13)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    # est_arr RELATIV zum Leg (Ankunft 15 min vor Plan) — ein hart kodiertes
    # Datum alterte hier täglich weiter und wurde ab >24h Abstand vom neuen
    # Facts-Physik-Gate (korrekt!) verworfen → Test kippte datumsabhängig.
    est_arr_iso = _iso(arr - timedelta(minutes=15))
    facts = {'est_arr': est_arr_iso, 'arr_status': 'Geplant'}
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: facts)
    monkeypatch.setattr(A, '_tail_recently_active', lambda r: True)
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False, status='Geplant')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] == 'landed'
    # Ist-Ankunft bleibt erhalten (keine erfundene, aber die echte est_arr steht).
    assert secs[0]['est_arr_iso'] == est_arr_iso


def test_geplant_corpse_without_est_arr_not_forced_landed(monkeypatch):
    # Gegenprobe: 'Geplant' OHNE Ist-Ankunft → NICHT auf 'landed' zwingen (könnte
    # ein echter Zukunfts-/verschobener Flug sein; nur mit belegter est_arr landen).
    dep = _now() - timedelta(hours=15)
    arr = _now() - timedelta(hours=13)
    secs = [_sector(flight='LH893', frm='RIX', to='FRA',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False, status='Geplant')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    assert secs[0]['status'] != 'landed'


# ══════════════════════════════════════════════════════════════════════════════
# FIX B — Stale-'landed' überlebt den FlightState-Engine-Override (LH890 LIVE)
# Live-vs-Test-Diskrepanz: die Stale-Regel feuerte in den Unit-Tests (Shadow aus),
# wurde live aber vom Engine-Block (Shadow/LIVE_FRIENDS an) mit 'airborne' wieder
# überschrieben, weil die Engine ohne Ankunfts-Position nur „airborne" projiziert.
# ══════════════════════════════════════════════════════════════════════════════
def test_stale_landed_survives_engine_override_lh890(monkeypatch):
    # LH890 FRA→RIX: nur dep 'Abgeflogen', keine arr-Obs, Plan-Ankunft 33 h vorbei.
    # Mit aktivem Engine-Block (FLIGHTSTATE_LIVE_FRIENDS=1) UND einer Engine-
    # Projektion, die (z.B. wegen stale aircraft_live-Position) 'airborne' liefert,
    # darf die überfällige Stale-Landung NICHT auf 'airborne' zurückgedreht werden.
    dep = _now() - timedelta(hours=33)
    arr = _now() - timedelta(hours=31)
    secs = [_sector(flight='LH890', frm='FRA', to='RIX',
                    dep_iso=_iso(dep), arr_iso=_iso(arr))]
    monkeypatch.setattr(ADB, '_flight_facts_from_obs', lambda *a, **k: {})
    monkeypatch.setenv('FLIGHTSTATE_LIVE_FRIENDS', '1')
    # Engine-Projektion hart auf 'airborne' zwingen (repliziert die Live-Situation,
    # in der eine stale Position die Engine noch „fliegt" projizieren lässt).
    import blueprints.flight_state as FS
    monkeypatch.setattr(FS, 'project_friend_leg',
                        lambda fs: {'status': 'EN_ROUTE'})
    import blueprints.warehouse_reader as WR
    monkeypatch.setitem(WR._ENGINE_PHASE_TO_LEGACY, 'EN_ROUTE', 'airborne')
    with patch.object(A, '_flight_obs_merged',
                      return_value=_merged(delay_known=False, status='Abgeflogen')):
        A._enrich_leg_delays(secs, dep.strftime('%Y-%m-%d'),
                             past_horizon_h=24 * 35)
    # Ohne den FIX-B-Guard würde 'airborne' die Stale-Landung überschreiben.
    assert secs[0]['status'] == 'landed'
