"""Plan-Blockzeiten erhalten (Task #23, Befund 2026-07-31).

Owner-Report Juli: LH sagt offiziell **57:35**, unsere Karte sagte 56:24.
Ursache ist nicht die Rechnung, sondern der VERLUST des Plans: LH mutiert die
duty_events-Zeiten selbst in place (nach dem Flug Ist-nah, ohne jedes
Provenienz-Feld), unser Import ersetzt die Zeile komplett — der Plan, also die
ABRECHNUNGSREFERENZ, wurde nie erfasst.

Diese Zelle pinnt die drei Teile des Fixes:
  (a) `_preserve_plan_times` — write-once, Erst-Stempel NUR für die Zukunft.
  (b) `flight_leg_details_plan` + `_pb_collect`/`_pb_plan_sum` — der
      gedrosselte Backfill-Weg für die Vergangenheit, inkl. Owner-Juli-Summe.
  (c) das ehrliche Quell-Etikett statt des pauschalen 'pdf'.

Und den Vertrag, der über allem steht (Owner-Regel „keine Fake-Werte"):
ein Leg ohne belegten Plan bleibt LEER — es wird nie aus dep_iso/arr_iso
rekonstruiert.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as A
from blueprints import lh_flightops as fo

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


def _sec(flight='LH440', frm='FRA', to='IAH', dep=None, arr=None, **extra):
    d = {'flight': flight, 'from': frm, 'to': to,
         'dep_iso': dep or '2026-07-25T08:00:00Z',
         'arr_iso': arr or '2026-07-25T18:30:00Z'}
    d.update(extra)
    return d


# ══════════════════════════════════════════════════════════════════════════════
# (a) _preserve_plan_times
# ══════════════════════════════════════════════════════════════════════════════
def test_zukunft_wird_beim_ersten_sehen_gestempelt():
    """Ein Leg, dessen Abflug noch bevorsteht, kann in dieser Antwort nur der
    PLAN sein — genau dann (und nur dann) darf gestempelt werden."""
    secs = [_sec(dep='2026-07-25T08:00:00Z', arr='2026-07-25T18:30:00Z')]
    A._preserve_plan_times(secs, None, now=NOW)
    assert secs[0]['sched_dep_iso'] == '2026-07-25T08:00:00Z'
    assert secs[0]['sched_arr_iso'] == '2026-07-25T18:30:00Z'


def test_vergangenheit_wird_nie_gestempelt():
    """DIE Regel gegen den synthetisierten Plan: der Historie-Loader holt bis
    zu sechs Monate rückwärts, dort steht LH-seitig längst Post-Op. Ohne
    dieses Gate würde er Ist-Werte als „Plan" festschreiben."""
    secs = [_sec(dep='2026-07-10T08:00:00Z', arr='2026-07-10T18:30:00Z')]
    A._preserve_plan_times(secs, None, now=NOW)
    assert 'sched_dep_iso' not in secs[0]
    assert 'sched_arr_iso' not in secs[0]


def test_write_once_ein_gespeicherter_plan_wird_nie_ueberschrieben():
    """Der Kern des Befunds: LH verschiebt die Zeit, wir übernehmen sie —
    aber der einmal festgehaltene Plan bleibt stehen."""
    prev = [_sec(dep='2026-07-10T10:25:00Z', arr='2026-07-10T12:40:00Z',
                 sched_dep_iso='2026-07-10T10:25:00Z',
                 sched_arr_iso='2026-07-10T12:40:00Z')]
    # LH liefert beim Re-Import verschobene Zeiten (Ist-nah).
    secs = [_sec(dep='2026-07-10T10:55:00Z', arr='2026-07-10T13:10:00Z')]
    A._preserve_plan_times(secs, prev, now=NOW)
    assert secs[0]['sched_dep_iso'] == '2026-07-10T10:25:00Z'
    assert secs[0]['sched_arr_iso'] == '2026-07-10T12:40:00Z'
    # Die neue (Ist-nahe) Zeit bleibt daneben stehen — sie wird nicht ersetzt.
    assert secs[0]['dep_iso'] == '2026-07-10T10:55:00Z'


def test_write_once_gilt_auch_fuer_ein_zukunfts_leg():
    """Auch in der Zukunft gewinnt der ALTE Plan — sonst wanderte er mit jeder
    LH-Verschiebung mit und wäre keine Referenz mehr."""
    prev = [_sec(dep='2026-07-25T08:00:00Z',
                 sched_dep_iso='2026-07-25T08:00:00Z',
                 sched_arr_iso='2026-07-25T18:30:00Z')]
    secs = [_sec(dep='2026-07-25T09:00:00Z', arr='2026-07-25T19:30:00Z')]
    A._preserve_plan_times(secs, prev, now=NOW)
    assert secs[0]['sched_dep_iso'] == '2026-07-25T08:00:00Z'


def test_leg_key_ist_whitespace_und_case_tolerant():
    prev = [_sec(flight='LH 440', sched_dep_iso='2026-07-25T08:00:00Z',
                 sched_arr_iso='2026-07-25T18:30:00Z')]
    secs = [_sec(flight='lh440')]
    A._preserve_plan_times(secs, prev, now=NOW)
    assert secs[0]['sched_dep_iso'] == '2026-07-25T08:00:00Z'


def test_doppelumlauf_ordnet_positionsstabil_zu():
    """Zwei Legs derselben Route am selben Tag: n-ter neuer Sektor ↔ n-ter
    alter Sektor (dieselbe Regel wie beim Flugnummern-Backfill)."""
    prev = [_sec(flight='LH100', frm='FRA', to='MUC',
                 sched_dep_iso='2026-07-25T06:00:00Z',
                 sched_arr_iso='2026-07-25T07:00:00Z'),
            _sec(flight='LH100', frm='FRA', to='MUC',
                 sched_dep_iso='2026-07-25T16:00:00Z',
                 sched_arr_iso='2026-07-25T17:00:00Z')]
    secs = [_sec(flight='LH100', frm='FRA', to='MUC'),
            _sec(flight='LH100', frm='FRA', to='MUC')]
    A._preserve_plan_times(secs, prev, now=NOW)
    assert secs[0]['sched_dep_iso'] == '2026-07-25T06:00:00Z'
    assert secs[1]['sched_dep_iso'] == '2026-07-25T16:00:00Z'


def test_geaenderte_route_erbt_keinen_fremden_plan():
    """Umlauf gestrichen, Ersatz-Tour am selben Tag: der Plan des ALTEN Legs
    darf nicht auf das neue Leg wandern."""
    prev = [_sec(flight='LH440', frm='FRA', to='IAH',
                 sched_dep_iso='2026-07-25T05:00:00Z',
                 sched_arr_iso='2026-07-25T15:30:00Z')]
    secs = [_sec(flight='LH400', frm='FRA', to='JFK')]
    A._preserve_plan_times(secs, prev, now=NOW)
    # Der eigene (Zukunfts-)Stempel, NICHT der Plan des gestrichenen Legs.
    assert secs[0]['sched_dep_iso'] == '2026-07-25T08:00:00Z'

    # Und in der Vergangenheit bleibt es leer statt fremd befüllt.
    prev_p = [_sec(flight='LH440', frm='FRA', to='IAH',
                   sched_dep_iso='2026-07-10T05:00:00Z')]
    secs_p = [_sec(flight='LH400', frm='FRA', to='JFK',
                   dep='2026-07-10T08:00:00Z')]
    A._preserve_plan_times(secs_p, prev_p, now=NOW)
    assert 'sched_dep_iso' not in secs_p[0]


def test_preserve_wirft_nie():
    assert A._preserve_plan_times(None, None) is None
    assert A._preserve_plan_times([None, 'x'], [None]) == [None, 'x']
    kaputt = [_sec(dep='nicht-iso')]
    A._preserve_plan_times(kaputt, None, now=NOW)
    assert 'sched_dep_iso' not in kaputt[0]


# ══════════════════════════════════════════════════════════════════════════════
# (a) durch die ECHTE Import-Pipeline (_attach_sectors)
# ══════════════════════════════════════════════════════════════════════════════
# Der Erst-Stempel gilt nur für die ZUKUNFT — die Pipeline-Zellen unten rechnen
# deshalb gegen die echte Uhr (+5 Tage), nicht gegen ein festes Datum.
FUT = (datetime.now(timezone.utc) + timedelta(days=5)).date()
FUT_TAG = FUT.strftime('%Y%m%d')
FUT_YMD = FUT.isoformat()


def _ics(dep_hhmm, arr_hhmm, tag=None):
    tag = tag or FUT_TAG
    return ('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//t//DE\r\n'
            'BEGIN:VEVENT\r\nUID:1\r\nSUMMARY:LH440 FRA-IAH\r\n'
            f'DTSTART:{tag}T{dep_hhmm}00Z\r\nDTEND:{tag}T{arr_hhmm}00Z\r\n'
            'END:VEVENT\r\nEND:VCALENDAR\r\n')


def test_attach_sectors_traegt_den_plan_ueber_den_reimport():
    """Ende-zu-Ende: `_ics_events_to_briefings` baut den Tag FRISCH auf (der
    alte Tages-Dict ist weg) — deshalb bekommt `_attach_sectors` den
    gespeicherten Stand jetzt als `existing` gereicht."""
    ev1 = A._parse_ics_to_events(_ics('0800', '1830'))
    b1, _ = A._ics_events_to_briefings(ev1, existing={})
    A._attach_sectors(b1, ev1, existing={})
    s1 = b1[FUT_YMD]['ical_sectors'][0]
    assert s1['sched_dep_iso'].startswith(FUT_YMD + 'T08:00')

    # LH verschiebt die Zeiten; Re-Import mit dem gespeicherten Stand.
    ev2 = A._parse_ics_to_events(_ics('0830', '1900'))
    b2, _ = A._ics_events_to_briefings(ev2, existing=dict(b1))
    A._attach_sectors(b2, ev2, existing=b1)
    s2 = b2[FUT_YMD]['ical_sectors'][0]
    assert s2['dep_iso'].startswith(FUT_YMD + 'T08:30')       # neue Zeit übernommen
    assert s2['sched_dep_iso'].startswith(FUT_YMD + 'T08:00')  # Plan gehalten


def test_attach_sectors_ohne_existing_bleibt_rueckwaertskompatibel():
    """`existing` ist optional — der Aufruf ohne bleibt gültig (Alt-Tests)."""
    ev = A._parse_ics_to_events(_ics('0800', '1830'))
    b, _ = A._ics_events_to_briefings(ev, existing={})
    A._attach_sectors(b, ev)
    assert b[FUT_YMD]['ical_sectors'][0]['from'] == 'FRA'


# ══════════════════════════════════════════════════════════════════════════════
# (b) Backfill-Weg: Parser, Sammler, Summe
# ══════════════════════════════════════════════════════════════════════════════
def test_leg_details_parser_gegen_die_echte_fixture():
    p = os.path.join(ROOT, 'tests', 'fixtures',
                     'flightops_COMMON_FLIGHT_LEG_DETAILS.json')
    with open(p) as f:
        resp = json.load(f)
    plan = fo.flight_leg_details_plan(resp)
    assert plan['sched_dep_iso'] == '2016-10-01T07:45:00Z'
    assert plan['sched_arr_iso'] == '2016-10-01T08:55:00Z'
    assert plan['dep'] == 'FRA' and plan['arr'] == 'JFK'
    assert plan['tail'] == 'DAISQ'


def test_leg_details_parser_liefert_nichts_statt_haelfte():
    assert fo.flight_leg_details_plan(None) is None
    assert fo.flight_leg_details_plan({}) is None
    assert fo.flight_leg_details_plan({'aircraftRegistration': 'DAIXY'}) is None
    # Eine der beiden Zeiten reicht — dann steht die andere ehrlich auf None.
    only_dep = fo.flight_leg_details_plan(
        {'scheduledTimeOfDeparture': '2026-07-01T10:25:00Z'})
    assert only_dep['sched_dep_iso'] == '2026-07-01T10:25:00Z'
    assert only_dep['sched_arr_iso'] is None


def test_block_minuten_und_tageswechsel():
    assert fo._pb_block_min({'sched_dep_iso': '2026-07-01T10:25:00Z',
                             'sched_arr_iso': '2026-07-01T12:40:00Z'}) == 135
    # Übernacht: Ankunft am Folgetag (der Feed liefert echtes UTC).
    assert fo._pb_block_min({'sched_dep_iso': '2026-07-01T22:00:00Z',
                             'sched_arr_iso': '2026-07-02T06:30:00Z'}) == 510
    # Ohne Plan KEINE Zahl — nie aus dep_iso/arr_iso rekonstruieren.
    assert fo._pb_block_min({'dep_iso': '2026-07-01T10:25:00Z',
                             'arr_iso': '2026-07-01T12:40:00Z'}) is None
    assert fo._pb_block_min(None) is None
    # Unplausibel (>20 h) wird verworfen statt verbucht.
    assert fo._pb_block_min({'sched_dep_iso': '2026-07-01T00:00:00Z',
                             'sched_arr_iso': '2026-07-01T21:00:00Z'}) is None


# Der Owner-Juli aus seinem Released Report: sechs Legs, Summe 57:35.
# Gepinnt sind die PLAN-BLOCKZEITEN und ihre Summe — die Strecken stehen nur
# als Träger daneben.
OWNER_JULI = [
    ('2026-07-04', 'LH716', 'FRA', 'HND', 12 * 60 + 40),
    ('2026-07-06', 'LH717', 'HND', 'FRA', 14 * 60 + 15),
    ('2026-07-12', 'LH582', 'FRA', 'CAI', 4 * 60 + 10),
    ('2026-07-13', 'LH583', 'CAI', 'FRA', 4 * 60 + 30),
    ('2026-07-24', 'LH454', 'FRA', 'SFO', 11 * 60 + 15),
    ('2026-07-26', 'LH455', 'SFO', 'FRA', 10 * 60 + 45),
]


def _owner_briefings(mit_plan=True):
    out = {}
    for d, fl, a, b, mins in OWNER_JULI:
        dep = datetime.fromisoformat(f'{d}T09:00:00+00:00')
        arr = dep + timedelta(minutes=mins)
        s = {'flight': fl, 'from': a, 'to': b,
             # dep_iso/arr_iso sind der LH-mutierte (Ist-nahe) Stand: 25 min
             # später — genau das Muster, das 56:24 statt 57:35 ergab.
             'dep_iso': (dep + timedelta(minutes=25)).isoformat().replace(
                 '+00:00', 'Z'),
             'arr_iso': (arr + timedelta(minutes=13)).isoformat().replace(
                 '+00:00', 'Z')}
        if mit_plan:
            s['sched_dep_iso'] = dep.isoformat().replace('+00:00', 'Z')
            s['sched_arr_iso'] = arr.isoformat().replace('+00:00', 'Z')
        out[d] = {'ical_sectors': [s]}
    return out


def test_owner_juli_plansumme_ist_5735():
    """DER BEWEIS-PIN: die sechs Legs seines Released Reports ergeben aus den
    sched_*-Zeiten exakt 57:35 — nicht 56:24."""
    all_secs, todo = fo._pb_collect(_owner_briefings(), '2026-07-01',
                                    '2026-07-31',
                                    now=datetime(2026, 8, 1,
                                                 tzinfo=timezone.utc))
    assert len(all_secs) == 6 and todo == []
    total, have, miss = fo._pb_plan_sum(all_secs)
    assert (have, miss) == (6, 0)
    assert total == 57 * 60 + 35
    assert fo._pb_hhmm(total) == '57:35'


def test_ohne_plan_zaehlt_der_monat_nichts_statt_falsch():
    """Ein Monat ohne gespeicherten Plan liefert 0:00 und sechs offene Legs —
    NICHT die Ist-Summe. Lieber eine sichtbare Lücke als eine falsche
    Abrechnungszahl."""
    b = _owner_briefings(mit_plan=False)
    all_secs, todo = fo._pb_collect(b, '2026-07-01', '2026-07-31',
                                    now=datetime(2026, 8, 1,
                                                 tzinfo=timezone.utc))
    assert len(todo) == 6                     # alle sechs sind Backfill-Fälle
    total, have, miss = fo._pb_plan_sum(all_secs)
    assert (total, have, miss) == (0, 0, 6)


def test_collect_laesst_zukunft_und_deadhead_aussen_vor():
    now = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
    b = {
        # Zukunft: der Import stempelt selbst, kein LH-Call nötig.
        '2026-07-25': {'ical_sectors': [_sec(dep='2026-07-25T08:00:00Z')]},
        # Deadhead zählt nicht in die Blockzeit (Released Report tut es auch nicht).
        '2026-07-10': {'ical_sectors': [_sec(dep='2026-07-10T08:00:00Z',
                                             dh=True)]},
        # Pseudo-Leg FRA-FRA (Simulator) ist kein Sektor.
        '2026-07-11': {'ical_sectors': [_sec(frm='FRA', to='FRA',
                                             dep='2026-07-11T08:00:00Z')]},
        # Echter Vergangenheits-Kandidat.
        '2026-07-12': {'ical_sectors': [_sec(dep='2026-07-12T08:00:00Z')]},
    }
    all_secs, todo = fo._pb_collect(b, '2026-07-01', '2026-07-31', now=now)
    assert sorted(d for d, _ in all_secs) == ['2026-07-12', '2026-07-25']
    assert [d for d, _ in todo] == ['2026-07-12']


def test_collect_respektiert_das_fenster():
    b = {'2026-06-30': {'ical_sectors': [_sec(dep='2026-06-30T08:00:00Z')]},
         '2026-07-01': {'ical_sectors': [_sec(dep='2026-07-01T08:00:00Z')]},
         '2026-08-01': {'ical_sectors': [_sec(dep='2026-08-01T08:00:00Z')]}}
    all_secs, _ = fo._pb_collect(b, '2026-07-01', '2026-07-31',
                                 now=datetime(2026, 9, 1, tzinfo=timezone.utc))
    assert [d for d, _ in all_secs] == ['2026-07-01']


# ══════════════════════════════════════════════════════════════════════════════
# (b) Endpoint — gedrosselt, on-demand, nie raten
# ══════════════════════════════════════════════════════════════════════════════
def _live_app():
    """Das app-Modul, das der Endpoint zur LAUFZEIT auflöst.

    SUITE-FALLE (gefunden 31.07. im vollen `make verify`): mehrere Testmodule
    laden app frisch (`del sys.modules['app']; import app` in
    tests/test_state_machine.py u. a.) — danach ist ein beim Import gebundenes
    `import app as A` ein STALE Objekt, während `blueprints.lh_flightops`
    intern `import app as _app` frisch auflöst. Ein Patch auf `A` ginge dann
    ins Leere und der Endpoint sähe leere Briefings (`legs=0`). Deshalb hier
    immer über sys.modules."""
    import importlib
    return sys.modules.get('app') or importlib.import_module('app')


def _backfill_env(monkeypatch, tmp_path, briefings, resp_for=None):
    saved = {}
    LA = _live_app()
    monkeypatch.setattr(LA, '_validate_token',
                        lambda _t: LA._TokenValidationResult(
                            LA._TokenValidationState.VALID, 'test@aerox.test'))
    monkeypatch.setattr(fo, '_access_state', lambda t: ('ok', 'ACC'))
    monkeypatch.setattr(LA, '_ical_briefings_load', lambda t: briefings)
    monkeypatch.setattr(LA, '_ical_briefings_save',
                        lambda t, b: saved.update(b) or True)
    monkeypatch.setattr(fo, '_flow_dir', lambda: str(tmp_path))
    monkeypatch.setattr(fo, '_pb_day_used', lambda now=None: 0)
    monkeypatch.setattr(fo, '_pb_budget_book', lambda now=None: None)
    monkeypatch.setattr(fo, '_PB_SPACING_S', 0)
    calls = []

    def _details(token, flight, date=None, dep=None, arr=None,
                 interactive=False, status_out=None):
        # Signatur spiegelt die ECHTE `fo.flight_leg_details` (inkl.
        # status_out — ein veralteter Mock ist ein Test-Infra-Bug).
        calls.append((flight, date))
        r = (resp_for or {}).get(flight)
        if isinstance(status_out, dict):
            status_out['kind'] = 'ok' if isinstance(r, dict) else 'error'
        return r
    monkeypatch.setattr(fo, 'flight_leg_details', _details)
    return saved, calls


def test_endpoint_backfillt_nur_vergangene_legs_ohne_plan(monkeypatch,
                                                          tmp_path):
    b = {'2026-07-12': {'ical_sectors': [
        _sec(flight='LH582', frm='FRA', to='CAI',
             dep='2026-07-12T09:25:00Z', arr='2026-07-12T13:48:00Z')]}}
    resp = {'LH582': {'scheduledTimeOfDeparture': '2026-07-12T09:00:00Z',
                      'scheduledTimeOfArrival': '2026-07-12T13:10:00Z',
                      'departureAirport': 'FRA', 'arrivalAirport': 'CAI'}}
    saved, calls = _backfill_env(monkeypatch, tmp_path, b, resp)

    r = _live_app().app.test_client().post(
        '/api/lh/flightops/plan-backfill/AT-OWNER',
                                 json={'from': '2026-07-01',
                                       'to': '2026-07-31'},
                                 headers={'Authorization': 'Bearer AT-OWNER'})
    d = r.get_json()
    assert r.status_code == 200 and d['ok'] is True
    assert calls == [('LH582', '2026-07-12')]
    assert d['written'] == 1 and d['calls'] == 1
    assert d['legs'][0]['status'] == 'ok'
    assert d['plan']['block_hhmm'] == '4:10'      # der PLAN, nicht 4:23
    # In den gespeicherten Sektor geschrieben.
    assert (saved['2026-07-12']['ical_sectors'][0]['sched_dep_iso']
            == '2026-07-12T09:00:00Z')


def test_endpoint_zweiter_lauf_kauft_nichts_nach(monkeypatch, tmp_path):
    """Der geteilte Plan-Cache hat KEIN TTL — die Plan-Zeit eines gewesenen
    Legs ändert sich nie. Ein zweiter Tap kostet 0 LH-Calls."""
    resp = {'LH582': {'scheduledTimeOfDeparture': '2026-07-12T09:00:00Z',
                      'scheduledTimeOfArrival': '2026-07-12T13:10:00Z'}}

    def _fresh():
        return {'2026-07-12': {'ical_sectors': [
            _sec(flight='LH582', frm='FRA', to='CAI',
                 dep='2026-07-12T09:25:00Z', arr='2026-07-12T13:48:00Z')]}}
    _s1, calls1 = _backfill_env(monkeypatch, tmp_path, _fresh(), resp)
    c = _live_app().app.test_client()
    body = {'from': '2026-07-01', 'to': '2026-07-31'}
    hdr = {'Authorization': 'Bearer AT-OWNER'}
    c.post('/api/lh/flightops/plan-backfill/AT-OWNER', json=body, headers=hdr)
    assert len(calls1) == 1

    _s2, calls2 = _backfill_env(monkeypatch, tmp_path, _fresh(), resp)
    d2 = c.post('/api/lh/flightops/plan-backfill/AT-OWNER', json=body,
                headers=hdr).get_json()
    assert calls2 == []                       # nichts nachgekauft
    assert d2['legs'][0]['status'] == 'cache'
    assert d2['plan']['block_hhmm'] == '4:10'


def test_endpoint_ohne_lh_antwort_wird_nichts_erfunden(monkeypatch, tmp_path):
    b = {'2026-07-12': {'ical_sectors': [
        _sec(flight='LH582', frm='FRA', to='CAI',
             dep='2026-07-12T09:25:00Z', arr='2026-07-12T13:48:00Z')]}}
    saved, calls = _backfill_env(monkeypatch, tmp_path, b, {'LH582': None})
    d = _live_app().app.test_client().post(
        '/api/lh/flightops/plan-backfill/AT-OWNER',
        json={'from': '2026-07-01', 'to': '2026-07-31'},
        headers={'Authorization': 'Bearer AT-OWNER'}).get_json()
    assert d['legs'][0]['status'] == 'error'
    assert d['legs'][0]['sched_dep_iso'] is None
    assert d['written'] == 0
    assert d['plan']['block_min'] == 0 and d['plan']['legs_without_plan'] == 1


def test_endpoint_deckelt_das_fenster(monkeypatch, tmp_path):
    _backfill_env(monkeypatch, tmp_path, {}, {})
    r = _live_app().app.test_client().post(
        '/api/lh/flightops/plan-backfill/AT-OWNER',
        json={'from': '2026-01-01', 'to': '2026-07-31'},
        headers={'Authorization': 'Bearer AT-OWNER'})
    assert r.status_code == 400
    assert r.get_json()['error'] == 'range_too_wide'


def test_endpoint_haelt_bei_erreichtem_tagesdeckel_an(monkeypatch, tmp_path):
    b = {'2026-07-12': {'ical_sectors': [
        _sec(flight='LH582', dep='2026-07-12T09:25:00Z')]}}
    _backfill_env(monkeypatch, tmp_path, b, {})
    monkeypatch.setattr(fo, '_pb_day_used',
                        lambda now=None: fo._PB_DAY_CEILING)
    d = _live_app().app.test_client().post(
        '/api/lh/flightops/plan-backfill/AT-OWNER',
        json={'from': '2026-07-01', 'to': '2026-07-31'},
        headers={'Authorization': 'Bearer AT-OWNER'}).get_json()
    assert d['legs'][0]['status'] == 'budget'
    assert d['calls'] == 0 and d['budget']['stopped'] is True


# ══════════════════════════════════════════════════════════════════════════════
# (c) Quell-Etikett
# ══════════════════════════════════════════════════════════════════════════════
def test_direkt_ics_schreibt_die_echte_quelle(monkeypatch):
    """Bis 31.07. stand für JEDEN Direkt-ICS-Import 'pdf' im Profil — auch für
    LH-FlightOps und den Geräte-Abruf. Die internen Aufrufer korrigierten das
    nur in ihrer HTTP-Antwort, nie im gespeicherten calendar_feed."""
    gespeichert = {}
    monkeypatch.setattr(A, '_profile_load', lambda t: {})
    monkeypatch.setattr(A, '_profile_load_from_disk', lambda t: {})
    monkeypatch.setattr(A, '_profile_save',
                        lambda t, p, full_disk_payload=None:
                        gespeichert.update(p) or True)
    monkeypatch.setattr(A, '_ical_briefings_load', lambda t: {})
    monkeypatch.setattr(A, '_ical_briefings_save', lambda t, b: True)
    monkeypatch.setattr(A, '_reconcile_month_briefings',
                        lambda *a, **k: {'feed_dates': 0, 'cleared': 0,
                                         'window': None})
    ics = _ics('0800', '1830')
    for hint, erwartet in (('flightops', 'flightops'), ('pdf', 'pdf'),
                           (None, 'ics_direct'), ('quatsch', 'ics_direct')):
        gespeichert.clear()
        body = {'ics_text': ics}
        if hint is not None:
            body['source'] = hint
        with A.app.test_request_context(json=body):
            A.import_calendar_feed('AT-X')
        assert gespeichert['calendar_feed']['source'] == erwartet, hint
        assert gespeichert['calendar_feed']['url'] == ''


def test_alle_direkt_quellen_teilen_das_verhalten():
    """Das Etikett wird ehrlich, das VERHALTEN bleibt gleich: alle drei
    Direkt-ICS-Quellen haben kein `url` und damit denselben 35-Tage-Schutz
    gegen das EK-Push-Reconcile."""
    assert set(A._DIRECT_ICS_SOURCES) == {'pdf', 'flightops', 'ics_direct'}
