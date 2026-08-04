"""Landing Report → Flugbuch-Abgleich (Welle 1, 2026-07-31) — rein OFFLINE.

Zwei Dinge werden hier festgenagelt, weil sie im Fehlerfall teuer sind:

1. `landingPerformed` ist PER-USER („die ANFRAGENDE Person hat die Landung
   durchgeführt") und ein STRING. Es heißt hier `self_landed`, damit es
   niemand als Flug-Status liest. Es darf NIE aus dem geteilten Cache kommen.
2. Das Flugbuch ist ein RECHTSDOKUMENT: dieser Pfad schreibt nie, er schlägt
   nur vor. Ein fehlender Report (HTTP 404 / response:null) ist 'pending' —
   NIEMALS „nicht gelandet".

Fixtures: die dokumentierte Mock-Shape (mit LH-Doku-Typo 'unkown') und eine
ECHTE PROD-Response vom 31.07. (tests/fixtures/lh_landing_report_prod.json,
pkNumber redigiert).
"""
import datetime as dtmod
import json
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as A  # noqa: E402  (Blueprint-Registrierung)
from blueprints import lh_flightops as fo  # noqa: E402

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')


def _prod_fixture():
    with open(os.path.join(_FIXTURES, 'lh_landing_report_prod.json')) as f:
        return json.load(f)


# Doku-Shape (Mock 2026-07-22) — trägt den LH-Doku-Typo 'unkown'.
DOC_LANDING = {
    "pkNumber": "123456A", "flightDesignator": "LH400",
    "flightDate": "2016-10-01Z", "departureAirport": "FRA",
    "destinationAirport": "XYZ", "tailsign": "DAISQ",
    "events": {"aircraft": {"out": "2016-10-01T10:04:00Z",
                            "off": "2016-10-01T10:18:00Z",
                            "on": "2016-10-01T13:44:00Z",
                            "in": "2016-10-01T14:02:00Z"}},
    "landingPerformed": "true", "lowVisibilityApproach": "unkown"}


# ══════════════════════════════════ Parser ══════════════════════════════════
def test_parse_real_prod_shape():
    """Echte PROD-Response (31.07.): alle vier OOOI-Zeiten, Block- UND
    Flugzeit, Ziel-Airport, Kennzeichen mit Bindestrich."""
    f = fo.landing_report_parse(_prod_fixture())
    assert f['self_landed'] is False          # STRING 'false' → echtes False
    assert f['tail'] == 'D-AISU'              # LH liefert 'DAISU'
    assert f['arr'] == 'BUD'                  # destinationAirport
    assert f['dep_iso'] == '2026-06-24T05:15:00Z'   # OUT
    assert f['arr_iso'] == '2026-06-24T06:43:00Z'   # IN
    assert f['off_iso'] == '2026-06-24T05:24:00Z'   # Rohwert off
    assert f['on_iso'] == '2026-06-24T06:38:00Z'    # Rohwert on
    assert f['block_min'] == 88               # 05:15 → 06:43
    assert f['air_min'] == 74                 # 05:24 → 06:38 (FCL.050)


def test_parse_lowvisibility_typo_and_prod_spelling_both_ignored():
    """`lowVisibilityApproach` ist deprecated. PROD sagt 'unknown', die Doku
    schreibt 'unkown' — beide Schreibweisen dürfen NICHTS am Ergebnis ändern
    und tauchen nirgends im Fakten-Dict auf."""
    base = _prod_fixture()
    typo = dict(base, lowVisibilityApproach='unkown')
    prod = dict(base, lowVisibilityApproach='unknown')
    missing = {k: v for k, v in base.items() if k != 'lowVisibilityApproach'}
    a, b, c = (fo.landing_report_parse(x) for x in (typo, prod, missing))
    assert a == b == c
    assert not [k for k in a if 'isib' in k.lower() or k.lower().startswith('lv')]


def test_parse_self_landed_string_to_bool():
    base = _prod_fixture()
    assert fo.landing_report_parse(dict(base, landingPerformed='true'))['self_landed'] is True
    assert fo.landing_report_parse(dict(base, landingPerformed='TRUE'))['self_landed'] is True
    assert fo.landing_report_parse(dict(base, landingPerformed='false'))['self_landed'] is False
    assert fo.landing_report_parse(dict(base, landingPerformed=True))['self_landed'] is True
    # Feld fehlt → None ('unbekannt'), NICHT False.
    missing = {k: v for k, v in base.items() if k != 'landingPerformed'}
    assert fo.landing_report_parse(missing)['self_landed'] is None


def test_parse_doc_shape_and_air_min():
    f = fo.landing_report_parse(DOC_LANDING)
    assert f['self_landed'] is True and f['tail'] == 'D-AISQ'
    assert f['block_min'] == 238 and f['air_min'] == 206
    assert 'landed' not in f          # alter Name ist weg


def test_air_min_survives_midnight_rollover():
    """off 23:50Z → on 01:10Z des FOLGETAGS = 80 min (nicht −1360)."""
    r = {"tailsign": "DAIHY", "destinationAirport": "JFK",
         "landingPerformed": "true",
         "events": {"aircraft": {"out": "2026-07-14T23:35:00Z",
                                 "off": "2026-07-14T23:50:00Z",
                                 "on": "2026-07-15T01:10:00Z",
                                 "in": "2026-07-15T01:22:00Z"}}}
    f = fo.landing_report_parse(r)
    assert f['air_min'] == 80
    assert f['block_min'] == 107


def test_parse_rejects_processing_errors_and_junk():
    assert fo.landing_report_parse({'processingErrors': [{'code': 500}]}) == {}
    assert fo.landing_report_parse(None) == {}
    assert fo.landing_report_parse('nope') == {}


# ═════════════════════════════ Kandidaten/Frische ═══════════════════════════
def _link(flight, date, dep, arr='MUC'):
    return {'service': 'landingreport',
            'params': {'flightDesignator': flight, 'flightDate': date + 'Z',
                       'departureAirport': dep, 'arrivalAirport': arr}}


def _day(back):
    return (dtmod.datetime.now(dtmod.timezone.utc).date()
            - dtmod.timedelta(days=back)).isoformat()


def test_candidates_freshness_gate():
    """Ankunft < 24 h ⇒ raus (LH hat den Report noch nicht), älter als das
    Fenster ⇒ raus. Dazwischen ⇒ Kandidat, aufsteigend nach Datum."""
    links = [_link('LH100', _day(0), 'FRA'),     # heute
             _link('LH101', _day(1), 'FRA'),     # gestern → zu frisch
             _link('LH102', _day(3), 'MUC'),     # drin
             _link('LH103', _day(13), 'FRA'),    # drin
             _link('LH104', _day(20), 'FRA'),    # außerhalb 14 Tage
             {'service': 'crewlist', 'params': {  # anderer Service
                 'flightDesignator': 'LH105', 'flightDate': _day(3) + 'Z',
                 'departureAirport': 'FRA'}}]
    got = fo._lb_candidates(links, 14)
    assert [c['flight'] for c in got] == ['LH103', 'LH102']
    assert got[1] == {'flight': 'LH102', 'date': _day(3), 'dep': 'MUC',
                      'arr': 'MUC'}


def test_candidates_dedupe_and_broken_rows():
    links = [_link('LH102', _day(3), 'FRA'), _link('LH102', _day(3), 'FRA'),
             {'service': 'landingreport', 'params': {'flightDate': _day(3)}},
             {'service': 'landingreport', 'params': {
                 'flightDesignator': 'LH9', 'flightDate': 'kaputt',
                 'departureAirport': 'FRA'}}]
    assert len(fo._lb_candidates(links, 14)) == 1


def _roster_day(back, sectors):
    return _day(back), {'ical_sectors': sectors}


def test_candidates_aus_roster_wenn_keine_landingreport_links():
    """DER BEFUND VOM 31.07.: im Link-Cache des Owners lag KEINE einzige
    landingReport-Referenz (nur flightInfo/crewList/…), der Abgleich lieferte
    deshalb `legs: []` bei `calls: 0` — er hatte nie einen Kandidaten. Der
    Roster kennt (Flugnummer, Datum, Abflug) für jedes geflogene Leg, und mehr
    braucht COMMON_LANDING_REPORT nicht."""
    briefings = dict([
        _roster_day(0, [{'flight': 'LH100', 'from': 'FRA', 'to': 'MUC'}]),
        _roster_day(1, [{'flight': 'LH101', 'from': 'FRA', 'to': 'MUC'}]),
        _roster_day(3, [{'flight': 'LH 454', 'from': 'FRA', 'to': 'SFO'}]),
        _roster_day(20, [{'flight': 'LH716', 'from': 'FRA', 'to': 'HND'}]),
    ])
    got = fo._lb_candidates_from_roster(briefings, 14)
    assert [c['flight'] for c in got] == ['LH454']       # 0/1 zu frisch, 20 zu alt
    assert got[0] == {'flight': 'LH454', 'date': _day(3), 'dep': 'FRA',
                      'arr': 'SFO'}


def test_roster_kandidaten_ohne_deadhead_und_ohne_halbe_zeilen():
    """Ein Deadhead hat keinen eigenen Landing Report, und ohne Flugnummer
    oder Abflugstation gibt es nichts zu fragen — beides fällt weg statt einen
    Leer-Call zu verbrennen."""
    briefings = dict([
        _roster_day(3, [{'flight': 'LH500', 'from': 'FRA', 'to': 'MUC', 'dh': True},
                        {'flight': '', 'from': 'FRA', 'to': 'MUC'},
                        {'flight': 'LH501', 'from': '', 'to': 'MUC'},
                        {'flight': 'LH502', 'from': 'FRA', 'to': 'FRA'},
                        {'flight': 'LH503', 'from': 'MUC', 'to': 'FRA'}]),
    ])
    assert [c['flight'] for c in fo._lb_candidates_from_roster(briefings, 14)] \
        == ['LH503']


def test_merge_haelt_link_kandidaten_vorn_und_dedupliziert():
    link_c = [{'flight': 'LH454', 'date': _day(3), 'dep': 'FRA', 'arr': 'SFO'}]
    roster_c = [{'flight': 'LH454', 'date': _day(3), 'dep': 'FRA', 'arr': 'SFO'},
                {'flight': 'LH455', 'date': _day(4), 'dep': 'SFO', 'arr': 'FRA'}]
    got = fo._lb_merge_candidates(link_c, roster_c)
    assert [c['flight'] for c in got] == ['LH455', 'LH454']   # sortiert nach Datum
    assert len(got) == 2


# ═══════════════════════════════ Cache-Semantik ═════════════════════════════
def test_shared_cache_never_stores_or_serves_per_user_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(fo, '_flow_dir', lambda: str(tmp_path))
    facts = fo.landing_report_parse(_prod_fixture())
    facts['self_landed'] = True          # per-User-Anteil …
    fo._lb_shared_put('LH1334|2026-06-24|FRA', facts)
    raw = json.load(open(os.path.join(str(tmp_path), 'folanding_shared.json')))
    assert 'self_landed' not in raw['LH1334|2026-06-24|FRA']
    assert 'pkNumber' not in raw['LH1334|2026-06-24|FRA']
    # … und selbst eine VON HAND vergiftete Datei kann ihn nicht einschleusen:
    raw['LH1334|2026-06-24|FRA']['self_landed'] = True
    with open(os.path.join(str(tmp_path), 'folanding_shared.json'), 'w') as f:
        json.dump(raw, f)
    got = fo._lb_shared_get('LH1334|2026-06-24|FRA')
    assert got['tail'] == 'D-AISU' and got['block_min'] == 88
    assert 'self_landed' not in got


def test_self_cache_is_per_user_and_only_bools(monkeypatch, tmp_path):
    monkeypatch.setattr(fo, '_flow_dir', lambda: str(tmp_path))
    k = 'LH1|2026-06-24|FRA'
    fo._lb_self_put('AT-ALICE', k, True)
    fo._lb_self_put('AT-BOB', k, False)
    assert fo._lb_self_get('AT-ALICE', k) is True
    assert fo._lb_self_get('AT-BOB', k) is False
    assert fo._lb_self_get('AT-CAROL', k) is None
    fo._lb_self_put('AT-CAROL', k, None)      # 'unbekannt' wird nicht konserviert
    assert fo._lb_self_get('AT-CAROL', k) is None


def test_shared_cache_ttl_expires(monkeypatch, tmp_path):
    monkeypatch.setattr(fo, '_flow_dir', lambda: str(tmp_path))
    now = 1_800_000_000.0
    fo._lb_shared_put('K', {'tail': 'D-AIXY'}, now=now)
    assert fo._lb_shared_get('K', now=now + 3600)['tail'] == 'D-AIXY'
    assert fo._lb_shared_get('K', now=now + 31 * 86400) is None


# ═════════════════════════════ Tages-Budget ═════════════════════════════════
def _reset_budget():
    fo._lb_day_memo.update(ts=0.0, day='', used=0)
    fo._lb_day_local.update(day='', n=0)


def test_own_day_counter_key_and_local_count(monkeypatch):
    _reset_budget()
    booked = []
    import blueprints.lh_open_api as lo
    monkeypatch.setattr(lo, 'budget_inc_key', lambda k, units=1: booked.append(k))
    monkeypatch.setattr('blueprints.aerox_data_blueprint._budget_key_used',
                        lambda k: 0)
    assert fo._lb_budget_key().startswith('lhfoD-landing:')
    fo._lb_budget_book()
    fo._lb_budget_book()
    assert booked == [fo._lb_budget_key()] * 2
    # Der persistierte Stand hinkt (Flusher alle 30 s) — der Prozess zählt mit.
    assert fo._lb_day_used() == 2
    _reset_budget()


def test_day_counter_takes_persisted_state(monkeypatch):
    _reset_budget()
    monkeypatch.setattr('blueprints.aerox_data_blueprint._budget_key_used',
                        lambda k: 397)
    assert fo._lb_day_used() == 397
    _reset_budget()


# ════════════════════════════════ Endpoint ══════════════════════════════════
def _auth(monkeypatch):
    monkeypatch.setattr(A, '_validate_token',
                        lambda _t: A._TokenValidationResult(
                            A._TokenValidationState.VALID, 'test@aerox.test'))


def _wire(monkeypatch, tmp_path, links, used=0):
    """Gemeinsame Verdrahtung: Auth ok, Grant ok, kein echter Netz-/SB-Zugriff,
    kein 0,7-s-Schlaf, Link-Cache vorgegeben."""
    _auth(monkeypatch)
    _reset_budget()
    monkeypatch.setattr(fo, '_flow_dir', lambda: str(tmp_path))
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('ok', 'ACC'))
    monkeypatch.setattr(fo, '_LB_SPACING_S', 0.0)
    monkeypatch.setattr(fo, '_links_load', lambda tok: links)
    monkeypatch.setattr(fo, '_links_save', lambda tok, l: None)
    monkeypatch.setattr(fo, 'duty_events',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('kein duty_events-Call erwartet')))
    monkeypatch.setattr(fo, '_lb_budget_book', lambda now=None: None)
    monkeypatch.setattr(fo, '_lb_day_used', lambda now=None: used)


def _post(days=None, token='AT-LBTEST'):
    body = {} if days is None else {'days': days}
    return A.app.test_client().post(
        f'/api/lh/flightops/logbook-verify/{token}',
        headers={'Authorization': 'Bearer ' + token}, json=body)


def test_endpoint_freshness_gate_excludes_fresh_leg(monkeypatch, tmp_path):
    """Ein Leg von gestern wird gar nicht erst abgefragt (LH liefert dafür
    response:null) — das Leg von vorgestern schon."""
    _wire(monkeypatch, tmp_path,
          [_link('LH101', _day(1), 'FRA'), _link('LH102', _day(3), 'MUC')])
    asked = []

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        asked.append(flight)
        if isinstance(status_out, dict):
            status_out['kind'] = 'ok'
        return dict(_prod_fixture(), landingPerformed='true')

    monkeypatch.setattr(fo, 'landing_report', _lr)
    r = _post()
    assert r.status_code == 200
    d = r.get_json()
    assert asked == ['LH102']
    assert [l['flight'] for l in d['legs']] == ['LH102']
    leg = d['legs'][0]
    assert leg['status'] == 'ok' and leg['self_landed'] is True
    assert leg['block_min'] == 88 and leg['air_min'] == 74
    assert leg['out_iso'] == '2026-06-24T05:15:00Z'
    assert leg['in_iso'] == '2026-06-24T06:43:00Z'
    assert leg['tail'] == 'D-AISU' and leg['arr'] == 'BUD'
    assert d['calls'] == 1 and d['budget']['ceiling'] == 400


def test_endpoint_404_is_pending_not_not_landed(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [_link('LH102', _day(3), 'MUC')])

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        if isinstance(status_out, dict):
            status_out.update(kind='http', code=404)
        return None

    monkeypatch.setattr(fo, 'landing_report', _lr)
    leg = _post().get_json()['legs'][0]
    assert leg['status'] == 'pending'
    assert leg['self_landed'] is None      # NICHT False!


def test_endpoint_null_response_is_pending(monkeypatch, tmp_path):
    """PROD antwortet für ein frisches Leg mit HTTP 200 + response:null."""
    _wire(monkeypatch, tmp_path, [_link('LH102', _day(3), 'MUC')])

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        if isinstance(status_out, dict):
            status_out['kind'] = 'ok'
        return None

    monkeypatch.setattr(fo, 'landing_report', _lr)
    assert _post().get_json()['legs'][0]['status'] == 'pending'


def test_endpoint_error_is_error(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [_link('LH102', _day(3), 'MUC')])

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        if isinstance(status_out, dict):
            status_out.update(kind='http', code=503)
        return None

    monkeypatch.setattr(fo, 'landing_report', _lr)
    assert _post().get_json()['legs'][0]['status'] == 'error'


def test_endpoint_own_day_ceiling_stops_and_reports(monkeypatch, tmp_path):
    """Eigener Tagesdeckel erreicht ⇒ KEIN LH-Call, jedes Leg meldet 'budget',
    und die Antwort sagt es explizit (kein stilles Kappen)."""
    _wire(monkeypatch, tmp_path,
          [_link('LH102', _day(3), 'MUC'), _link('LH103', _day(4), 'FRA')],
          used=fo._LB_DAY_CEILING)
    monkeypatch.setattr(fo, 'landing_report',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('Deckel erreicht — kein Call!')))
    d = _post().get_json()
    assert [l['status'] for l in d['legs']] == ['budget', 'budget']
    assert d['calls'] == 0
    assert d['budget']['stopped'] is True and d['budget']['stop_reason'] == 'budget'
    assert d['budget']['used'] == fo._LB_DAY_CEILING


def test_endpoint_global_budget_stop_marks_rest(monkeypatch, tmp_path):
    """Reißt der GLOBALE lhfo-Deckel mitten im Lauf, wird der Rest ehrlich als
    'budget' ausgewiesen statt 30-mal ins Leere zu laufen."""
    _wire(monkeypatch, tmp_path,
          [_link('LH10%d' % i, _day(3 + i), 'FRA') for i in range(3)])
    calls = []

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        calls.append(flight)
        if isinstance(status_out, dict):
            status_out['kind'] = 'day_budget'
        return None

    monkeypatch.setattr(fo, 'landing_report', _lr)
    d = _post().get_json()
    assert len(calls) == 1                     # nur EIN Fehlversuch
    assert [l['status'] for l in d['legs']] == ['budget'] * 3
    assert d['calls'] == 0                     # gar nicht gesendet → nicht gebucht


def test_endpoint_caps_calls_per_request(monkeypatch, tmp_path):
    """Mehr Kandidaten als der Per-Aufruf-Deckel: Rest = 'skipped_budget'."""
    links = [_link('LH2%03d' % i, _day(2 + (i % 25)), 'FRA') for i in range(40)]
    _wire(monkeypatch, tmp_path, links)
    n = []

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        n.append(flight)
        if isinstance(status_out, dict):
            status_out['kind'] = 'ok'
        return _prod_fixture()

    monkeypatch.setattr(fo, 'landing_report', _lr)
    d = _post(days=31).get_json()
    assert len(n) == fo._LB_MAX_CALLS == 30
    st = [l['status'] for l in d['legs']]
    assert st.count('ok') == 30 and st.count('skipped_budget') == len(links) - 30
    assert d['calls'] == 30


def test_shared_cache_serves_times_but_never_the_per_user_flag(monkeypatch,
                                                               tmp_path):
    """Der geteilte Cache liefert Zeiten/Kennzeichen — `self_landed` bleibt
    None, solange der EIGENE Abruf nichts geliefert hat."""
    _wire(monkeypatch, tmp_path, [_link('LH1334', _day(3), 'FRA')])
    key = fo._lb_key('LH1334', _day(3), 'FRA')
    facts = fo.landing_report_parse(dict(_prod_fixture(),
                                         landingPerformed='true'))
    fo._lb_shared_put(key, facts)             # Kollege war schon da

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        if isinstance(status_out, dict):
            status_out.update(kind='http', code=404)   # für UNS noch nicht da
        return None

    monkeypatch.setattr(fo, 'landing_report', _lr)
    leg = _post().get_json()['legs'][0]
    assert leg['status'] == 'pending'
    assert leg['block_min'] == 88 and leg['tail'] == 'D-AISU'
    assert leg['self_landed'] is None          # NIE aus dem geteilten Cache


def test_per_user_flag_always_from_own_response(monkeypatch, tmp_path):
    """Der Kollege hat gelandet (self_landed=true im Umlauf) — unsere eigene
    Response sagt 'false'. Es MUSS False herauskommen."""
    _wire(monkeypatch, tmp_path, [_link('LH1334', _day(3), 'FRA')])
    key = fo._lb_key('LH1334', _day(3), 'FRA')
    fo._lb_shared_put(key, fo.landing_report_parse(_prod_fixture()))
    # Geteilte Datei von Hand vergiften — darf trotzdem nicht durchschlagen.
    p = os.path.join(str(tmp_path), 'folanding_shared.json')
    raw = json.load(open(p))
    raw[key]['self_landed'] = True
    with open(p, 'w') as f:
        json.dump(raw, f)

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        if isinstance(status_out, dict):
            status_out['kind'] = 'ok'
        return dict(_prod_fixture(), landingPerformed='false')

    monkeypatch.setattr(fo, 'landing_report', _lr)
    leg = _post().get_json()['legs'][0]
    assert leg['status'] == 'ok' and leg['self_landed'] is False


def test_second_run_is_free_and_stays_user_scoped(monkeypatch, tmp_path):
    """Zweiter Tap desselben Users: Zeiten aus dem geteilten Cache, eigenes
    Flag aus der EIGENEN Datei ⇒ 0 LH-Calls. Ein ANDERER User bekommt daraus
    NICHT das fremde Flag — er muss selbst fragen."""
    links = [_link('LH1334', _day(3), 'FRA')]
    _wire(monkeypatch, tmp_path, links)
    seen = []

    def _lr(tok, flight, date, dep, interactive=False, status_out=None):
        seen.append(tok)
        if isinstance(status_out, dict):
            status_out['kind'] = 'ok'
        return dict(_prod_fixture(),
                    landingPerformed='true' if tok == 'AT-ALICE' else 'false')

    monkeypatch.setattr(fo, 'landing_report', _lr)
    a1 = _post(token='AT-ALICE').get_json()['legs'][0]
    a2 = _post(token='AT-ALICE').get_json()['legs'][0]
    assert a1['self_landed'] is True and a2['self_landed'] is True
    assert seen == ['AT-ALICE']              # zweiter Lauf ohne LH-Call
    b1 = _post(token='AT-BOB').get_json()['legs'][0]
    assert seen == ['AT-ALICE', 'AT-BOB']    # Bob MUSS selbst fragen
    assert b1['self_landed'] is False
    assert b1['block_min'] == 88             # Zeiten kamen geteilt


def test_endpoint_reloads_window_on_link_cache_miss(monkeypatch, tmp_path):
    """Leerer Link-Cache (nach jedem Deploy der Normalfall): EIN
    duty_events-Call für das GANZE Fenster, dann die Legs daraus."""
    _wire(monkeypatch, tmp_path, [])
    windows = []

    def _duty(tok, fd, td, interactive=False):
        windows.append((fd, td, interactive))
        return {'rosterDays': [{'events': [{'_links': {'landingReport': {
            'href': 'https://x/COMMON_LANDING_REPORT?flightDesignator=LH777'
                    '&flightDate=' + _day(5) + 'Z&departureAirport=FRA'}}}]}]}

    monkeypatch.setattr(fo, 'duty_events', _duty)
    monkeypatch.setattr(fo, 'landing_report',
                        lambda tok, f, d, dep, interactive=False,
                        status_out=None: (
                            status_out.update(kind='ok') if isinstance(
                                status_out, dict) else None) or _prod_fixture())
    d = _post().get_json()
    assert len(windows) == 1 and windows[0][2] is True
    assert windows[0] == (_day(14), _day(0), True)
    assert [l['flight'] for l in d['legs']] == ['LH777']


def test_days_param_clamped(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [_link('LH102', _day(2), 'MUC')])
    monkeypatch.setattr(fo, 'landing_report',
                        lambda tok, f, d, dep, interactive=False,
                        status_out=None: (
                            status_out.update(kind='ok') if isinstance(
                                status_out, dict) else None) or _prod_fixture())
    assert _post(days=999).get_json()['days'] == fo._LB_MAX_DAYS == 31
    assert _post(days='kaputt').get_json()['days'] == 14
    assert _post(days=None).get_json()['days'] == fo._LB_DEFAULT_DAYS == 14
    # Unter dem Frische-Gate ergibt kein Fenster Sinn → dorthin geklemmt.
    assert _post(days=1).get_json()['days'] == fo._LB_MIN_AGE_DAYS


# ══════════════════════════════════ Auth ════════════════════════════════════
def test_auth_gate_requires_bearer_binding(monkeypatch, tmp_path):
    """Owner-scoped: ein FREMDER Bearer darf den Pfad-Token nie ansprechen
    (gilt in beiden Gate-Modi). Ob ein FEHLENDER Bearer blockt, entscheidet
    AEROX_REQUIRE_TOKEN_BINDING — hier gegen den aktiven Modus geprüft."""
    _wire(monkeypatch, tmp_path, [])
    c = A.app.test_client()
    r = c.post('/api/lh/flightops/logbook-verify/AT-LBTEST',
               headers={'Authorization': 'Bearer AT-SOMEONE-ELSE'}, json={})
    assert r.status_code == 401
    assert r.get_json()['error'] == 'token_binding_mismatch'
    if A._BUG004_REQUIRE_TOKEN_BINDING:
        r2 = c.post('/api/lh/flightops/logbook-verify/AT-LBTEST', json={})
        assert r2.status_code == 401
        assert r2.get_json()['error'] == 'token_binding_required'


def test_cached_completion_proofs_are_actual_fresh_and_batch_read_once(monkeypatch):
    now_dt = dtmod.datetime(2026, 8, 3, 22, 0, tzinfo=dtmod.timezone.utc)
    now = now_dt.timestamp()
    calls = []
    shared = {
        fo._lb_key('LH2129', '2026-08-03', 'DRS'): {
            'arr_iso': '2026-08-03T20:45:00Z', 'self_landed': True,
            'ts': now - 10,
        },
        fo._lb_key('LH586', '2026-08-03', 'MUC'): {
            'on_iso': '2026-08-04T00:05:00Z', 'ts': now - 10,
        },
        fo._lb_key('LH400', '2026-07-01', 'FRA'): {
            'arr_iso': '2026-07-01T18:30:00Z',
            'ts': now - fo._LB_SHARED_TTL_S - 1,
        },
    }

    def _load(_path):
        calls.append(_path)
        return shared

    monkeypatch.setattr(fo, '_lb_json_load', _load)
    result = fo.logbook_cached_completion_proofs([
        {'flight': 'LH2129', 'date': '2026-08-03', 'dep': 'DRS'},
        {'flight': 'LH586', 'date': '2026-08-03', 'dep': 'MUC'},
        {'flight': 'LH400', 'date': '2026-07-01', 'dep': 'FRA'},
    ], now=now)

    assert result == {
        ('LH2129', '2026-08-03', 'DRS'): '2026-08-03T20:45:00Z',
    }
    assert len(calls) == 1


def test_auth_gate_rejects_unknown_token(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [])
    monkeypatch.setattr(A, '_validate_token',
                        lambda _t: A._TokenValidationResult(
                            A._TokenValidationState.INVALID, None))
    assert _post().status_code == 401


def test_not_connected_and_refresh_pending(monkeypatch, tmp_path):
    _wire(monkeypatch, tmp_path, [])
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('none', None))
    r = _post()
    assert r.status_code == 401 and r.get_json()['error'] == 'not_connected'
    monkeypatch.setattr(fo, '_access_state', lambda tok: ('pending', None))
    r = _post()
    assert r.status_code == 503
    assert r.get_json()['error'] == 'token_refresh_pending'


def test_no_write_to_logbook_anywhere():
    """Wächter: dieser Pfad darf NIE ins Flugbuch schreiben (Rechtsdokument).
    Der Endpoint-Code enthält keinen einzigen Logbook-/Leg-Save-Aufruf."""
    import inspect
    body = inspect.getsource(fo.flightops_logbook_verify)
    for forbidden in ('logbook_save', 'logbook_put', 'save_leg', 'insert(',
                      'upsert(', '_profile_save', 'import_calendar_feed'):
        assert forbidden not in body, forbidden
