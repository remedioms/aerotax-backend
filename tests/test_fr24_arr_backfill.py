"""FR24-Ankunfts-Backfill für VERGANGENE Legs an NICHT geharvesteten Airports
(TLL/RIX/BOS/CMN/ARN/GVA …).

Owner-Prinzip „einmal gefragt, dann für immer gespeichert": ein vergangenes Leg
ohne <arr>#ARR-Board-Row bekommt seine Ist-Landung über GENAU EINE bezahlte
flight-summary — das Ergebnis wird als echte airport_delay_obs-Row persistiert,
sodass jede Wiederholung durch den Row-Dedupe (Prüfung b) KOSTENLOS ist.

Beweist:
  • Lücke + past ⇒ genau EIN Summary-Call + Row geschrieben + Facts liefern est_arr.
  • Zweiter Aufruf ⇒ NULL weitere Calls (die geschriebene Row füllt die Lücke).
  • Zukunfts-/heutiges Leg ⇒ kein Call.
  • Summary leer ⇒ Negative-Cache verhindert Wiederholung (1 Call/6 h/Prozess).
  • Credit-Deckel erschöpft ⇒ kein Call (Summary liefert None).
  • Flag AUS (Default) ⇒ kein Call.
"""
import types

import pytest

import blueprints.aerox_data_blueprint as axd
from blueprints.aerox_data_blueprint import _flight_facts_from_obs


class _FakeQ:
    """Chainbarer PostgREST-Stub. Merkt sich, ob es ein #ARR-Select ist, damit die
    Query je nach Airport-Filter die passenden Rows liefert. insert() hängt an den
    geteilten Store an (persistiert für die Re-Read-Runde)."""

    def __init__(self, sb):
        self._sb = sb
        self._is_insert = None
        self._eq = {}          # column → value (nur die Filter, die wir brauchen)

    def select(self, *a, **kw):
        return self

    def insert(self, payload):
        self._is_insert = payload
        return self

    def eq(self, col, val):
        self._eq[col] = val
        return self

    def __getattr__(self, name):
        # in_/order/limit/… → chainbar (ignoriert)
        def _chain(*a, **kw):
            return self
        return _chain

    def execute(self):
        if self._is_insert is not None:
            self._sb.rows.append(dict(self._is_insert))
            self._sb.inserts.append(dict(self._is_insert))
            return types.SimpleNamespace(data=[self._is_insert])
        # Select: die relevanten eq-Filter (airport/flight) anwenden, damit die
        # Exists-Prüfungen (_fr24_arr_obs_exists/_fr24_dep_obs_exists) korrekt sind.
        rows = self._sb.rows
        if 'airport' in self._eq:
            rows = [r for r in rows if r.get('airport') == self._eq['airport']]
        if 'flight' in self._eq:
            rows = [r for r in rows if r.get('flight') == self._eq['flight']]
        return types.SimpleNamespace(data=list(rows))


class _FakeSB:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserts = []

    def table(self, name):
        return _FakeQ(self)


@pytest.fixture(autouse=True)
def _clear_neg_cache():
    axd._FR24_ARR_BACKFILL_NEG.clear()
    yield
    axd._FR24_ARR_BACKFILL_NEG.clear()


def _enable(monkeypatch, summary, *, calls):
    """Flag scharf, FR24 verfügbar, Museums-Wächter neutral, gemockter Summary-
    Client der Aufrufe zählt."""
    monkeypatch.setenv('FR24_ARR_BACKFILL', '1')
    monkeypatch.setattr(axd, '_fr24_available', lambda: True)
    monkeypatch.setattr(axd, '_tail_active_guard', lambda r: True)

    def _fake_by_number(fn, date=None):
        calls.append((fn, date))
        return summary

    monkeypatch.setattr(axd, '_fr24_flight_by_number', _fake_by_number)


# TLL (Tallinn) — nicht geharvestet. Vergangenes Leg LH882 TLL→FRA am 2026-07-14.
# Summary im flight_status-Schema von _fr24_flight_by_number: sched_dep/sched_arr =
# FR24 Ist-Ab/-Ankunft (absolut-UTC), est_* immer None.
_PAST = '2026-07-14'
_SUMMARY_LANDED = {
    'flight': 'LH882', 'dep_iata': 'TLL', 'arr_iata': 'FRA',
    'sched_dep': '2026-07-14T05:10:00Z',   # 08:10 TLL (Sommer +03)
    'sched_arr': '2026-07-14T06:55:00Z',   # 08:55 FRA (Sommer +02)
    'est_dep': None, 'est_arr': None, 'status': 'landed',
}


def test_gap_and_past_writes_exactly_one_row_and_facts_have_est_arr(monkeypatch):
    calls = []
    sb = _FakeSB(rows=[])          # kein Board an TLL/FRA für diesen Flug
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    _enable(monkeypatch, _SUMMARY_LANDED, calls=calls)

    f = _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')

    # Genau EIN Summary-Call.
    assert len(calls) == 1
    # Genau eine ARR-Row geschrieben (dep-Seite zusätzlich erlaubt, aber ARR ist Pflicht).
    arr_inserts = [r for r in sb.inserts if r['airport'] == 'FRA#ARR']
    assert len(arr_inserts) == 1
    row = arr_inserts[0]
    # Format EXAKT wie echte Board-Rows: bare 'HH:MM' station-lokal (FRA +02 → 08:55).
    assert row['sched'] == '08:55'
    assert row['esti'] == '08:55'
    assert row['date'] == '2026-07-14'
    assert row['status'] == 'Gelandet'
    assert row['dest_iata'] == 'TLL'
    # Facts sehen die frisch geschriebene Ankunft → est_arr gesetzt, Route komplett.
    assert f['est_arr'] == '2026-07-14T08:55:00+02:00'
    assert f['sched_arr'] == '2026-07-14T08:55:00+02:00'
    assert f['arr_iata'] == 'FRA'


def test_second_call_costs_zero_credits_via_row_dedupe(monkeypatch):
    calls = []
    sb = _FakeSB(rows=[])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    _enable(monkeypatch, _SUMMARY_LANDED, calls=calls)

    _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert len(calls) == 1          # erster Aufruf zahlt
    # Der zweite Aufruf findet die geschriebene esti-<arr>#ARR-Row (Prüfung b) →
    # KEIN weiterer Summary-Call, obwohl der Negative-Cache leer wäre.
    axd._FR24_ARR_BACKFILL_NEG.clear()
    f2 = _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert len(calls) == 1          # unverändert — Row-Dedupe machte es kostenlos
    assert f2['est_arr'] == '2026-07-14T08:55:00+02:00'


def test_future_or_today_leg_never_calls(monkeypatch):
    calls = []
    sb = _FakeSB(rows=[])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    _enable(monkeypatch, _SUMMARY_LANDED, calls=calls)

    import time
    today = time.strftime('%Y-%m-%d', time.gmtime())
    _flight_facts_from_obs('LH882', today, 'TLL', 'FRA')
    assert calls == []              # heutiges Leg: Ankunft evtl. noch nicht vorbei
    future = '2099-01-01'
    _flight_facts_from_obs('LH882', future, 'TLL', 'FRA')
    assert calls == []


def test_empty_summary_sets_negative_cache(monkeypatch):
    calls = []
    sb = _FakeSB(rows=[])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    _enable(monkeypatch, {}, calls=calls)     # Summary leer (kein Treffer)

    _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert len(calls) == 1
    assert not sb.inserts                       # nichts geschrieben (nichts erfunden)
    # Negative-Cache verhindert die Wiederholung im selben Prozess/Fenster.
    _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert len(calls) == 1


def test_credit_cap_exhausted_no_call(monkeypatch):
    calls = []
    sb = _FakeSB(rows=[])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    _enable(monkeypatch, _SUMMARY_LANDED, calls=calls)
    # Der Deckel steckt in _fr24_flight_by_number: bei erschöpftem Budget liefert es
    # None OHNE HTTP-Call. Das simulieren wir, indem der gemockte Client None gibt.
    monkeypatch.setattr(axd, '_fr24_flight_by_number', lambda fn, date=None: None)

    f = _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert not sb.inserts
    assert 'est_arr' not in f


def test_flag_off_is_default_no_call(monkeypatch):
    calls = []
    sb = _FakeSB(rows=[])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    monkeypatch.setattr(axd, '_fr24_available', lambda: True)
    monkeypatch.setattr(axd, '_tail_active_guard', lambda r: True)
    monkeypatch.delenv('FR24_ARR_BACKFILL', raising=False)   # Default AUS

    def _fake(fn, date=None):
        calls.append(fn)
        return _SUMMARY_LANDED
    monkeypatch.setattr(axd, '_fr24_flight_by_number', _fake)

    _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert calls == []
    assert not sb.inserts


def test_existing_arr_board_row_blocks_backfill(monkeypatch):
    """Existiert bereits eine echte <arr>#ARR-Row MIT esti (geharvestetes Ziel),
    wird KEIN Credit gezogen — die Lücke ist schon gefüllt."""
    calls = []
    existing = {'airport': 'FRA#ARR', 'flight': 'LH882', 'dest_iata': 'TLL',
                'date': _PAST, 'sched': '08:50', 'esti': '08:58',
                'status': 'Gelandet', 'max_delay_min': 8}
    sb = _FakeSB(rows=[existing])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    _enable(monkeypatch, _SUMMARY_LANDED, calls=calls)

    f = _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert calls == []                          # arr_cands nicht leer → gar kein Versuch
    assert not sb.inserts
    assert f['est_arr'] == '2026-07-14T08:58:00+02:00'


def test_wrong_route_summary_not_written(monkeypatch):
    """Ein Summary-Treffer mit falschem/umgeleitetem Ziel darf keine fremde
    Ankunft in die ARR-Zeile schreiben."""
    calls = []
    sb = _FakeSB(rows=[])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    wrong = dict(_SUMMARY_LANDED, arr_iata='MUC')
    _enable(monkeypatch, wrong, calls=calls)

    _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    assert len(calls) == 1
    assert not sb.inserts                       # Route-Mismatch → nichts geschrieben


def test_dep_side_also_written_when_summary_has_actual_departure(monkeypatch):
    calls = []
    sb = _FakeSB(rows=[])
    monkeypatch.setattr(axd, '_sb', lambda: sb)
    _enable(monkeypatch, _SUMMARY_LANDED, calls=calls)

    _flight_facts_from_obs('LH882', _PAST, 'TLL', 'FRA')
    dep_inserts = [r for r in sb.inserts if r['airport'] == 'TLL']
    assert len(dep_inserts) == 1
    # TLL Sommer +03: 05:10Z → 08:10 lokal, bare.
    assert dep_inserts[0]['sched'] == '08:10'
    assert dep_inserts[0]['dest_iata'] == 'FRA'
