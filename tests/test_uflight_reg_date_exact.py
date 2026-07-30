"""Datums-treue Registrierung im Unified-Resolver (/api/ax/uflight).

ROOT-CAUSE (Owner 2026-07-30): `uflight/LH454?date=2026-07-28` lieferte
`reg DABYP` — den Tail, der HEUTE fliegt. Korrekt für den 28.07. ist DABYH
(Warehouse `flights`: tail DABYH, conf 0.95).

Warum: `_resolve_unified_flight_core` bezog `reg` aus `_aircraft_live_flight`,
einem DATUMSLOSEN Jetzt-Schnappschuss (einziger Filter `updated_at > now−40min`;
die Funktion hat gar keinen `date`-Parameter). Danach schnitt
`reg = reg or facts.get('reg')` die EINE date-exakte Quelle kurz
(`_flight_facts_from_obs`: `.eq('date', d)` plus LH-flightstatus für genau
diesen Tag). Der Museums-Tail-Wächter greift hier prinzipiell nicht — der
heutige Tail ist ja aktiv.

Gefixt über `_live_snapshot_covers_date` (Gate am Seed) + `_warehouse_day_tail`
(date-exakter Fallback aus `flights`). Für den HEUTIGEN Tag bleibt alles wie es
war — das prüfen die Tests ausdrücklich mit.
"""
import time
from unittest.mock import patch

import blueprints.aerox_data_blueprint as BP
import blueprints.warehouse_reader as WR


def _today():
    return time.strftime('%Y-%m-%d', time.gmtime())


def _past():
    return time.strftime('%Y-%m-%d', time.gmtime(time.time() - 2 * 86400))


# ══════════════════════════════════════════════════════════════════════════════
# _live_snapshot_covers_date — reines Gate
# ══════════════════════════════════════════════════════════════════════════════
def test_live_snapshot_covers_today():
    assert BP._live_snapshot_covers_date(_today()) is True


def test_live_snapshot_does_not_cover_a_past_day():
    assert BP._live_snapshot_covers_date('2026-07-28',
                                         now=time.mktime((2026, 7, 30, 12, 0, 0, 0, 0, 0))) is False


def test_live_snapshot_no_date_means_now():
    """Ohne ?date meint der Aufrufer „jetzt" → Schnappschuss bleibt gültig."""
    assert BP._live_snapshot_covers_date(None) is True
    assert BP._live_snapshot_covers_date('') is True


def test_live_snapshot_fails_closed_on_garbage():
    """Fail-closed: ist das Datum nicht nachweislich heute, gilt der
    Schnappschuss nicht. Kostet höchstens eine fehlende Reg — billiger als eine
    falsche Maschine."""
    assert BP._live_snapshot_covers_date('nicht-ein-datum') is False


# ══════════════════════════════════════════════════════════════════════════════
# _warehouse_day_tail — date-exakter Tail aus `flights`
# ══════════════════════════════════════════════════════════════════════════════
class _Resp:
    def __init__(self, data):
        self.data = data


class _FlightsSB:
    """Minimaler Supabase-Stub, der die gestellten Filter mitschreibt."""

    def __init__(self, rows):
        self._rows = rows
        self.filters = {}

    def table(self, name):
        self.filters['table'] = name
        return self

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters[col] = val
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return _Resp(self._rows)


def test_warehouse_day_tail_is_filtered_on_the_exact_service_date():
    sb = _FlightsSB([{'tail': 'D-ABYH'}])
    with patch.object(BP, '_sb', return_value=sb):
        assert BP._warehouse_day_tail('LH454', '2026-07-28') == 'DABYH'
    assert sb.filters['table'] == 'flights'
    assert sb.filters['op_flight_no'] == 'LH454'
    assert sb.filters['service_date'] == '2026-07-28'   # KEINE Aufweitung


def test_warehouse_day_tail_none_when_the_day_has_no_row():
    with patch.object(BP, '_sb', return_value=_FlightsSB([])):
        assert BP._warehouse_day_tail('LH454', '2026-07-28') is None


def test_warehouse_day_tail_needs_flight_and_date():
    with patch.object(BP, '_sb', return_value=_FlightsSB([{'tail': 'D-ABYH'}])):
        assert BP._warehouse_day_tail('LH454', None) is None
        assert BP._warehouse_day_tail('', '2026-07-28') is None


def test_warehouse_day_tail_survives_sb_down():
    with patch.object(BP, '_sb', return_value=None):
        assert BP._warehouse_day_tail('LH454', '2026-07-28') is None


# ══════════════════════════════════════════════════════════════════════════════
# _resolve_unified_flight_core — die Kaskade als Ganzes
# ══════════════════════════════════════════════════════════════════════════════
_ALF_TODAY = {'callsign': 'DLH454', 'flight': 'LH454', 'reg': 'DABYP',
              'dep_iata': 'FRA', 'arr_iata': 'SFO', 'aircraft': 'B748'}


def _resolve(date, facts=None, warehouse_tail=None):
    with patch.object(BP, '_aircraft_live_flight', return_value=dict(_ALF_TODAY)), \
            patch.object(WR, 'route_for_flight', return_value={}), \
            patch.object(BP, '_flight_facts_from_obs', return_value=(facts or {})), \
            patch.object(BP, '_warehouse_day_tail', return_value=warehouse_tail), \
            patch.object(BP, '_tail_active_guard', return_value=True):
        return BP._resolve_unified_flight_core('LH454', date, False, None, None, False)


def test_past_day_does_not_serve_todays_tail():
    """DER Owner-Fall: der Jetzt-Schnappschuss (DABYP) darf für den 28.07.
    nicht gelten — die date-exakten Fakten (DABYH) gewinnen."""
    res = _resolve(_past(), facts={'reg': 'DABYH', 'type': 'B748'})
    assert res['identity']['reg'] == 'DABYH'
    assert res['aircraft']['reg'] == 'DABYH'


def test_past_day_falls_back_to_the_warehouse_tail():
    """Kennen die Board-Fakten den Tag nicht, liefert `flights` ihn — nie der
    Jetzt-Schnappschuss."""
    res = _resolve(_past(), facts={}, warehouse_tail='DABYH')
    assert res['identity']['reg'] == 'DABYH'


def test_past_day_without_any_dated_source_stays_empty():
    """Keine date-exakte Quelle ⇒ KEINE Reg. Lieber nichts als die falsche
    Maschine (Owner-Regel „keine Fake-Werte")."""
    res = _resolve(_past(), facts={}, warehouse_tail=None)
    assert res['identity']['reg'] is None
    assert res['aircraft']['reg'] is None
    # Die Route bleibt: eine Flugnummer fliegt Tag für Tag dieselbe Strecke,
    # nur die MASCHINE wechselt.
    assert (res['route'] or {}).get('origin', {}).get('iata') == 'FRA'
    assert (res['route'] or {}).get('destination', {}).get('iata') == 'SFO'


def test_today_still_uses_the_live_snapshot_unchanged():
    """Regressions-Riegel: für HEUTE bleibt der Live-Schnappschuss die Quelle
    (er ist dort die frischeste) — inklusive Muster."""
    res = _resolve(_today(), facts={})
    assert res['identity']['reg'] == 'DABYP'
    assert res['aircraft']['type'] == 'B748'


def test_today_warehouse_read_is_skipped(monkeypatch):
    """Kosten-Riegel: für heute wird `flights` gar nicht erst gefragt."""
    calls = []
    with patch.object(BP, '_aircraft_live_flight', return_value={'flight': 'LH454',
                                                                'dep_iata': 'FRA',
                                                                'arr_iata': 'SFO'}), \
            patch.object(WR, 'route_for_flight', return_value={}), \
            patch.object(BP, '_flight_facts_from_obs', return_value={}), \
            patch.object(BP, '_warehouse_day_tail',
                         side_effect=lambda *a: calls.append(a)), \
            patch.object(BP, '_tail_active_guard', return_value=True):
        BP._resolve_unified_flight_core('LH454', _today(), False, None, None, False)
    assert calls == []
