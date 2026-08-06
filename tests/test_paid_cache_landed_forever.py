"""„Gelandet = für immer gecacht" — Schreibseite des bezahlten FR24-Caches.

Vorfall 2026-08-05: 7.992 von 8.000 FR24-Tages-Credits verbraucht, ~3.785
Lookups auf nur 1.311 verschiedene call_keys. Jeder Flug wurde also im Schnitt
~3× pro Tag bezahlt, weil die positive Ergebnis-TTL (6 h) Re-Polls erlaubt. Für
einen Flug IN DER LUFT ist das richtig (Ist-Zeiten ändern sich noch), für einen
GELANDETEN Flug in einem abgeschlossenen Zeitfenster ist jede Wiederholung
verschwendetes Geld — die Ist-Zeiten sind endgültig.

Getestet (alles gemockt/offline — kein Netz-Call, kein Supabase, keine Credits):
  • gelandet + geschlossenes Fenster → result_until ≥ +300 Tage + Log-Marker
  • nur Schätz-Ankunft (kein datetime_landed)  → unveränderte 6-h-TTL
  • in der Luft (Ist-Abflug, keine Ankunft)    → unveränderte 6-h-TTL
  • noch offenes Fenster trotz gelandeter Zeile → unveränderte 6-h-TTL
  • Negative-Cache (leeres Ergebnis)           → unverändert, kein Stempel
  • Lese-Pfad: permanenter Eintrag = Cache-HIT ohne zweiten Provider-Call
"""
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app  # noqa: F401,E402 — registriert sys.modules['app'] für _life_app
import blueprints.aerox_data_blueprint as BP  # noqa: E402
from blueprints import paid_cost_control as PCC  # noqa: E402


_ICAO2IATA = {'EDDF': 'FRA', 'KJFK': 'JFK'}
_SIX_HOURS = 6 * 3600


@pytest.fixture(autouse=True)
def _clean_state():
    BP._MEM_BUDGET.clear()
    BP._FR24_REG_CACHE.clear()
    PCC.reset_local_state()
    yield
    BP._MEM_BUDGET.clear()
    BP._FR24_REG_CACHE.clear()
    PCC.reset_local_state()


def _day(delta_days):
    """UTC-Datum relativ zu heute — die Tests dürfen nicht an einem fixen
    Kalendertag hängen (Vergangenheit muss Vergangenheit BLEIBEN)."""
    return (datetime.now(timezone.utc)
            + timedelta(days=delta_days)).strftime('%Y-%m-%d')


def _iso(delta_hours):
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)
            ).strftime('%Y-%m-%dT%H:%M:%SZ')


def _summary(tko, ldg, *, ended=True, extra=None):
    row = {
        'flight': 'LH400', 'callsign': 'DLH400', 'type': 'A346', 'reg': 'D-AIHW',
        'orig_icao': 'EDDF', 'dest_icao': 'KJFK', 'dest_icao_actual': None,
        'datetime_takeoff': tko, 'datetime_landed': ldg, 'flight_ended': ended,
    }
    row.update(extra or {})
    return row


def _paid_row():
    """Die eine geschriebene Zeile des prozesslokalen Paid-Caches."""
    rows = list(PCC._LOCAL_CALLS.values())
    assert len(rows) == 1, rows
    return rows[0]


def _lookup(rows, date, caplog=None):
    """Ein bezahlter Flugnummern-Lookup gegen ein gemocktes FR24."""
    ctx = caplog.at_level(logging.INFO) if caplog is not None else None
    with patch.object(BP, '_sb', return_value=None), \
         patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=_ICAO2IATA.get), \
         patch.object(BP, '_fr24_get', return_value={'data': rows}) as mget:
        if ctx is not None:
            with ctx:
                out = BP._fr24_flight_by_number('LH400', date)
        else:
            out = BP._fr24_flight_by_number('LH400', date)
    return out, mget


def test_landed_flight_is_cached_quasi_forever(caplog):
    """Echte Ist-Landung in der Vergangenheit + abgeschlossenes Tagesfenster:
    das Ergebnis kann sich nie mehr ändern → kein weiterer Credit dafür."""
    day = _day(-2)
    out, mget = _lookup([_summary(day + 'T09:00:00Z', day + 'T17:00:00Z')],
                        day, caplog=caplog)
    assert out and out['dep_iata'] == 'FRA' and out['arr_iata'] == 'JFK'
    assert mget.call_count == 1
    row = _paid_row()
    assert row['result_until'] - time.time() >= 300 * 86400
    assert row['negative_until'] == 0 and row['negative_reason'] is None
    assert '[paid-cache] landed-forever' in caplog.text
    assert 'key=fr24:summary:' in caplog.text
    assert 'arr=' + day + 'T17:00:00Z' in caplog.text


def test_estimated_arrival_never_counts_as_landed(caplog):
    """Nur eine SCHÄTZ-Ankunft (kein datetime_landed) ist kein Landebeleg —
    geschätzte Zeiten wandern noch, also bleibt es bei der bisherigen TTL."""
    day = _day(-2)
    row_in = _summary(day + 'T09:00:00Z', None,
                      extra={'estimated_arrival': day + 'T17:00:00Z',
                             'eta': day + 'T17:00:00Z'})
    _out, mget = _lookup([row_in], day, caplog=caplog)
    assert mget.call_count == 1
    row = _paid_row()
    left = row['result_until'] - time.time()
    assert 0 < left <= _SIX_HOURS
    assert 'landed-forever' not in caplog.text


def test_airborne_flight_keeps_normal_ttl(caplog):
    """Ist-Abflug da, Ankunft fehlt → der Flug ist unterwegs. Genau hier IST der
    Re-Poll richtig: die Ist-Ankunft entsteht erst noch."""
    day = _day(-2)
    _out, mget = _lookup([_summary(day + 'T09:00:00Z', None, ended=False)],
                         day, caplog=caplog)
    assert mget.call_count == 1
    left = _paid_row()['result_until'] - time.time()
    assert 0 < left <= _SIX_HOURS
    assert 'landed-forever' not in caplog.text


def test_open_window_keeps_normal_ttl_even_when_rows_landed(caplog):
    """Heutiges Fenster reicht bis 23:59: es können später WEITERE Zeilen
    dazukommen. Ein Einfrieren würde den nächsten Flug verschwinden lassen."""
    today = _day(0)
    _out, mget = _lookup([_summary(_iso(-6), _iso(-3))], today, caplog=caplog)
    assert mget.call_count == 1
    left = _paid_row()['result_until'] - time.time()
    assert 0 < left <= _SIX_HOURS
    assert 'landed-forever' not in caplog.text


def test_negative_cache_path_unchanged(caplog):
    """Leeres Ergebnis bleibt ein reason-spezifischer Negative-Cache — kein
    Ergebnis-Eintrag, kein Permanent-Stempel."""
    day = _day(-2)
    out, mget = _lookup([], day, caplog=caplog)
    assert out is None and mget.call_count == 1
    row = _paid_row()
    assert row['result'] is None and row['result_until'] == 0
    assert row['negative_reason'] == 'not_found'
    assert 0 < row['negative_until'] - time.time() <= 12 * 3600
    assert 'landed-forever' not in caplog.text


def test_permanent_entry_still_serves_reads_without_new_paid_call():
    """Lese-Pfad unverändert: result_until wird respektiert — ein permanenter
    Eintrag liefert Monate später denselben Payload OHNE zweiten Provider-Call.
    (Direkt auf der Kontrollschicht, weil hier die Uhr gestellt werden kann.)"""
    now = [1_000_000.0]
    calls = []
    payload = {'data': [{'flight': 'LH400', 'datetime_landed': '2026-08-04T17:00:00Z'}]}

    def _fetch():
        calls.append(1)
        return payload

    def _run():
        return PCC.paid_fetch(
            sb=None, call_key='fr24:summary:deadbeef', provider='fr24',
            day_key='fr24:d', month_key='fr24m:m', reserve_units=3,
            day_cap=8000, month_cap=200000, fetch=_fetch,
            actual_units=lambda _p: 2, budget_used=lambda _k: 0,
            budget_adjust=lambda _k, _d: None, positive_ttl=_SIX_HOURS,
            landed_probe=lambda _p: '2026-08-04T17:00:00Z',
            permanent_ttl=365 * 86400, allow_local=True,
            clock=lambda: now[0], sleeper=lambda _s: None)

    first = _run()
    assert first.source == 'upstream' and len(calls) == 1
    now[0] += 200 * 86400                      # weit jenseits der 6-h-TTL
    second = _run()
    assert second.source == 'shared_cache' and second.payload == payload
    assert len(calls) == 1                     # kein zweiter bezahlter Call


def test_normal_ttl_entry_expires_and_pays_again():
    """Kontrollprobe zur vorigen: ohne Landebeleg bleibt es beim Ablauf nach der
    bisherigen TTL — die Regel greift NUR für belegte Landungen."""
    now = [1_000_000.0]
    calls = []

    def _fetch():
        calls.append(1)
        return {'data': [{'flight': 'LH400'}]}

    def _run():
        return PCC.paid_fetch(
            sb=None, call_key='fr24:summary:cafe', provider='fr24',
            day_key='fr24:d', month_key='fr24m:m', reserve_units=3,
            day_cap=8000, month_cap=200000, fetch=_fetch,
            actual_units=lambda _p: 2, budget_used=lambda _k: 0,
            budget_adjust=lambda _k, _d: None, positive_ttl=_SIX_HOURS,
            landed_probe=lambda _p: None, allow_local=True,
            clock=lambda: now[0], sleeper=lambda _s: None)

    assert _run().source == 'upstream'
    now[0] += _SIX_HOURS + 1
    assert _run().source == 'upstream'
    assert len(calls) == 2


def test_probe_defect_never_loses_the_paid_payload():
    """Eine werfende Sonde darf das bereits BEZAHLTE Ergebnis nicht kosten —
    sie bedeutet nur: normale TTL."""
    now = [1_000_000.0]
    out = PCC.paid_fetch(
        sb=None, call_key='fr24:summary:boom', provider='fr24',
        day_key='fr24:d', month_key='fr24m:m', reserve_units=3,
        day_cap=8000, month_cap=200000,
        fetch=lambda: {'data': [{'flight': 'LH400'}]},
        actual_units=lambda _p: 2, budget_used=lambda _k: 0,
        budget_adjust=lambda _k, _d: None, positive_ttl=_SIX_HOURS,
        landed_probe=lambda _p: (_ for _ in ()).throw(ValueError('boom')),
        allow_local=True, clock=lambda: now[0], sleeper=lambda _s: None)
    assert out.source == 'upstream' and out.payload['data']
    assert PCC._LOCAL_CALLS['fr24:summary:boom']['result_until'] == (
        now[0] + _SIX_HOURS)
