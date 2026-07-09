"""FR24 official API client (bezahlter LETZTER Fallback hinter Warehouse/Boards/
gRPC). Owner 2026-07-09: freies Scraping bleibt Hauptquelle; FR24 nur für Lücken;
jeder Credit permanent gespeichert; Karte NIE über FR24 (live-positions=120cr).

Getestet (alles gemockt/offline — kein echter Netz-Call, keine Credits):
  • _fr24_summary_to_leg  — Record→Leg-Dict (ICAO→IATA, Dauer, Reg, Umleitung).
  • _fr24_flights_by_reg  — Normalisierung, Sortierung, Limit, Budget-Zählung.
  • Guards: kein Token → [], Budget aus → [] (kein Call).
  • _fr24_hyphenate_reg   — DAINV→D-AINV.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from unittest.mock import patch
import pytest

import app  # noqa: F401 — registriert sys.modules['app'] für _life_app
import blueprints.aerox_data_blueprint as BP


@pytest.fixture(autouse=True)
def _reset_budget():
    BP._MEM_BUDGET.clear()
    BP._FR24_REG_CACHE.clear()
    yield
    BP._MEM_BUDGET.clear()
    BP._FR24_REG_CACHE.clear()


_ICAO2IATA = {'EDDF': 'FRA', 'KJFK': 'JFK', 'KIAD': 'IAD', 'LEBL': 'BCN',
              'EPWA': 'WAW', 'EKCH': 'CPH', 'KRSW': 'RSW', 'EDDM': 'MUC'}


def _summary(flight, orig, dest, tko, ldg, reg='D-AIHW', typ='A346',
             ended=True, dest_actual=None):
    return {
        'flight': flight, 'callsign': 'DLH', 'type': typ, 'reg': reg,
        'orig_icao': orig, 'dest_icao': dest, 'dest_icao_actual': dest_actual,
        'datetime_takeoff': tko, 'datetime_landed': ldg,
        'flight_ended': ended,
    }


def test_summary_to_leg_maps_fields():
    with patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)):
        leg = BP._fr24_summary_to_leg(_summary(
            'LH400', 'EDDF', 'KJFK',
            '2026-07-08T09:00:00Z', '2026-07-08T17:00:00Z'))
    assert leg['flight_no'] == 'LH400'
    assert leg['src'] == 'FRA' and leg['dst'] == 'JFK'
    assert leg['duration_min'] == 8 * 60          # 09:00Z → 17:00Z = 480
    assert leg['reg'] == 'DAIHW'                   # ohne Bindestrich (Warehouse-Form)
    assert leg['type'] == 'A346'
    assert leg['status'] == 'landed'
    assert leg['diverted'] is False
    assert leg['source'] == 'fr24'


def test_summary_to_leg_diversion_uses_actual_dest():
    with patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)):
        leg = BP._fr24_summary_to_leg(_summary(
            'SK1415', 'EDDF', 'EKCH',
            '2026-07-08T05:00:00Z', '2026-07-08T06:00:00Z',
            dest_actual='EPWA'))                    # umgeleitet nach Warschau
    assert leg['dst'] == 'WAW'
    assert leg['diverted'] is True


def test_summary_to_leg_inflight_no_duration():
    """Noch in der Luft (kein datetime_landed) → keine Dauer, status None."""
    with patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)):
        leg = BP._fr24_summary_to_leg(_summary(
            'LH400', 'EDDF', 'KJFK',
            '2026-07-08T09:00:00Z', None, ended=False))
    assert leg['duration_min'] is None
    assert leg['status'] is None


def test_flights_by_reg_normalizes_sorts_limits():
    resp = {'data': [
        _summary('LH418', 'EDDF', 'KIAD', '2026-07-07T11:00:00Z', '2026-07-07T19:20:00Z'),
        _summary('LH400', 'EDDF', 'KJFK', '2026-07-08T09:00:00Z', '2026-07-08T17:00:00Z'),
        _summary('LH401', 'KJFK', 'EDDF', '2026-07-08T19:00:00Z', '2026-07-09T02:09:00Z'),
    ]}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp) as mget:
        legs = BP._fr24_flights_by_reg('D-AIHW', days=4, limit=2)
    # by-registration Call ging raus …
    assert mget.called
    assert mget.call_args[0][0] == '/flight-summary/light'
    assert mget.call_args[0][1]['registrations'] == 'D-AIHW'
    # … Ergebnis neueste-zuerst + Limit 2
    assert [l['flight_no'] for l in legs] == ['LH401', 'LH400']
    # Credits PRO Ergebnis gezählt (3 Treffer → 3), nicht pauschal.
    assert BP._budget_key_used(BP._fr24_budget_key()) == 3


def test_no_token_returns_empty_no_call():
    with patch.object(BP, '_fr24_token', return_value=''), \
         patch.object(BP, '_fr24_get') as mget:
        assert BP._fr24_flights_by_reg('D-AIHW') == []
        mget.assert_not_called()


def test_budget_exhausted_returns_empty_no_call():
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.dict(os.environ, {'FR24_DAILY_CREDIT_CAP': '2'}), \
         patch.object(BP, '_fr24_get') as mget:
        BP._budget_key_inc(BP._fr24_budget_key(), 2)   # Deckel erreicht
        assert BP._fr24_flights_by_reg('D-AIHW') == []
        mget.assert_not_called()


def test_hyphenate_reg():
    assert BP._fr24_hyphenate_reg('DAINV') == 'D-AINV'
    assert BP._fr24_hyphenate_reg('D-AINV') == 'D-AINV'
    assert BP._fr24_hyphenate_reg('') is None


def test_cache_prevents_double_spend():
    """Zweiter Lookup derselben Maschine → KEIN zweiter Call, KEINE Extra-Credits."""
    resp = {'data': [_summary('LH400', 'EDDF', 'KJFK',
                              '2026-07-08T09:00:00Z', '2026-07-08T17:00:00Z')]}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp) as mget:
        a = BP._fr24_flights_by_reg('D-AIHW', days=4, limit=5)
        b = BP._fr24_flights_by_reg('D-AIHW', days=4, limit=5)
    assert a == b and len(a) == 1
    assert mget.call_count == 1                     # nur EIN echter Call
    assert BP._budget_key_used(BP._fr24_budget_key()) == 2   # nur EINMAL Credits


def test_empty_result_is_negative_cached():
    """FR24 kennt die Maschine nicht → leeres Ergebnis wird auch gecacht."""
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value={'data': []}) as mget:
        assert BP._fr24_flights_by_reg('D-ZZZZ') == []
        assert BP._fr24_flights_by_reg('D-ZZZZ') == []
    assert mget.call_count == 1


def test_flight_by_number_returns_status_schema():
    resp = {'data': [_summary('LH400', 'EDDF', 'KJFK',
                              '2026-07-08T09:00:00Z', '2026-07-08T17:00:00Z')]}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp):
        f = BP._fr24_flight_by_number('LH400', '2026-07-08')
    assert f['flight'] == 'LH400'
    assert f['dep_iata'] == 'FRA' and f['arr_iata'] == 'JFK'
    assert f['duration_min'] == 480
    assert f['reg'] == 'DAIHW' and f['aircraft'] == 'A346'
    assert f['callsign'] == 'DLH'          # echter Funkname für Live-Position


def test_flight_by_number_none_when_no_data():
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_fr24_get', return_value={'data': []}):
        assert BP._fr24_flight_by_number('LH999', '2026-07-08') is None


def test_flights_by_airline_only_with_reg():
    """Airline-Bulk: nur Legs MIT echter Reg (crowdsourcebar), operating_as-Filter,
    Credits pro Ergebnis."""
    resp = {'data': [
        _summary('4Y100', 'EDDF', 'LEBL', '2026-07-09T06:00:00Z',
                 '2026-07-09T08:05:00Z', reg='D-AIKO'),
        _summary('4Y101', 'LEBL', 'EDDF', '2026-07-09T09:00:00Z',
                 '2026-07-09T11:05:00Z', reg=''),          # keine Reg → raus
    ]}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp) as mget:
        # kleines Fenster → 1 Chunk, damit der Mock nicht 24× denselben liefert
        legs = BP._fr24_flights_by_airline('OCN', days=1, chunk_hours=48)
    assert mget.call_args[0][1]['operating_as'] == 'OCN'
    # dedup per fr24_id/flight+takeoff, nur Legs MIT Reg
    assert len(legs) == 1 and legs[0]['flight_no'] == '4Y100'
    assert BP._budget_key_used(BP._fr24_budget_key()) >= 2   # >=1 Chunk gezählt


def test_flight_by_callsign_resolves_true_flight():
    """Funkname OCN601 → echter Flug 4Y60 FRA→RSW A333 D-AIKO (nicht das falsche
    4Y601). callsigns-Filter, callsign im Ergebnis."""
    resp = {'data': [_summary('4Y60', 'EDDF', 'KRSW', '2026-07-09T12:00:00Z',
                              None, reg='D-AIKO', typ='A333', ended=False)]}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp) as mget:
        f = BP._fr24_flight_by_callsign('OCN601')
    assert mget.call_args[0][1]['callsigns'] == 'OCN601'
    assert f['flight'] == '4Y60' and f['dep_iata'] == 'FRA' and f['arr_iata'] == 'RSW'
    assert f['reg'] == 'DAIKO' and f['aircraft'] == 'A333' and f['callsign'] == 'OCN601'


def test_flight_by_callsign_none_when_no_data():
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_fr24_get', return_value={'data': []}):
        assert BP._fr24_flight_by_callsign('OCN999') is None


def test_flights_by_airline_dedups_across_chunks():
    """Mehrere Zeit-Chunks liefern denselben Flug → nur EINMAL im Ergebnis."""
    resp = {'data': [_summary('4Y100', 'EDDF', 'LEBL', '2026-07-09T06:00:00Z',
                              '2026-07-09T08:05:00Z', reg='D-AIKO')]}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp) as mget:
        legs = BP._fr24_flights_by_airline('OCN', days=1, chunk_hours=6)
    assert mget.call_count > 1                 # geblättert
    assert len(legs) == 1                      # trotzdem nur 1 (dedup)


def test_flight_by_number_date_none_normalized_to_today_key():
    """Cache-Key-Divergenz-Fix: date=None wird auf den heutigen UTC-Tag
    normalisiert → flight_status (mit Datum) und resolve-flight (ohne) treffen
    denselben Hard-Cache-Eintrag = nur EIN bezahlter Call."""
    import time as _t
    resp = {'data': [_summary('LH400', 'EDDF', 'KJFK',
                              '2026-07-08T09:00:00Z', '2026-07-08T17:00:00Z')]}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp) as mget:
        a = BP._fr24_flight_by_number('LH400')                       # ohne Datum
        b = BP._fr24_flight_by_number(
            'LH400', _t.strftime('%Y-%m-%d', _t.gmtime()))           # heutiges Datum
    assert a == b and a is not None
    assert mget.call_count == 1


def test_monthly_cap_blocks_budget():
    """Monats-Zweitschlüssel: FR24_MONTHLY_CREDIT_CAP deckelt auch dann, wenn
    der Tages-Cap noch frei ist."""
    with patch.dict(os.environ, {'FR24_MONTHLY_CREDIT_CAP': '2'}):
        BP._budget_key_inc(BP._fr24_month_budget_key(), 2)
        assert BP._fr24_budget_ok() is False


def test_budget_inc_counts_day_and_month():
    BP._fr24_budget_inc(3)
    assert BP._budget_key_used(BP._fr24_budget_key()) == 3
    assert BP._budget_key_used(BP._fr24_month_budget_key()) == 3


def test_uflight_paid_miss_negative_cached():
    """?paid=1-Miss wird 12 h negativ gecacht: derselbe unauflösbare Query
    schaltet paid beim nächsten Resolve NICHT wieder scharf (kein Re-Spend)."""
    BP._UFLIGHT_MEMO.clear()
    BP._UFLIGHT_PAID_MISS.clear()
    calls = []

    def fake_core(q, date, csq, lat, lon, allow_paid, date_auto=False):
        calls.append(allow_paid)
        return {'ok': True, 'found': False, 'query': q, 'date': date}

    with patch.object(BP, '_resolve_unified_flight_core', side_effect=fake_core):
        BP.resolve_unified_flight('XX9NEG', date='2026-07-09', allow_paid=True)
        BP.resolve_unified_flight('XX9NEG', date='2026-07-09', allow_paid=True)
    assert calls == [True, False]
    BP._UFLIGHT_MEMO.clear()
    BP._UFLIGHT_PAID_MISS.clear()


def test_flights_by_airline_respects_watermark_and_reports_cover():
    """Prewarm-Resume: t_from (Watermark) verkürzt das Fenster; cover_out['to']
    trägt die letzte abgedeckte Grenze für die nächste Persistierung."""
    import time as _t
    resp = {'data': [_summary('4Y100', 'EDDF', 'LEBL',
                              '2026-07-09T06:00:00Z', '2026-07-09T08:05:00Z')]}
    now = _t.time()
    cover = {}
    with patch.object(BP, '_fr24_token', return_value='tok'), \
         patch.object(BP, '_icao_to_iata', side_effect=lambda c: _ICAO2IATA.get(c)), \
         patch.object(BP, '_fr24_get', return_value=resp) as mget:
        BP._fr24_flights_by_airline('OCN', days=2, chunk_hours=2,
                                    t_from=now - 3600, cover_out=cover)
    # Watermark 1 h zurück + 2h-Chunks → genau EIN Chunk statt 24
    assert mget.call_count == 1
    assert cover.get('to') and abs(cover['to'] - now) < 5


def test_fr24_cache_evicts_oldest_quarter_not_all():
    """LRU-Eviction statt clear(): die jüngsten Einträge (und damit die teuer
    bezahlten frischen Treffer) überleben."""
    BP._FR24_REG_CACHE.clear()
    import time as _t
    base = _t.time()
    for i in range(501):
        BP._FR24_REG_CACHE['k%d' % i] = (base + i, None)
    BP._fr24_cache_evict()
    assert 300 < len(BP._FR24_REG_CACHE) < 501
    assert 'k500' in BP._FR24_REG_CACHE      # jüngster Eintrag bleibt
    assert 'k0' not in BP._FR24_REG_CACHE    # ältester Eintrag flog raus
