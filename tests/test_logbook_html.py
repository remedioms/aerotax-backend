"""Alte HTML-Logbuch-Route `/api/user/logbook-html/<token>`.

Sie bleibt bestehen (ausgelieferte Builds rufen sie weiter auf), muss aber
dasselbe zeigen wie Flugbuch-Ansicht und PDF: Blockzeit nach der
Ist-Differenz-Regel, Landungen/Starts, und KEINE erfundene
Standby-Markierung. Rein offline — dieselben Seeds wie test_logbook.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as backend
from tests.test_logbook import TOKEN, _seed, _seed_import, BLZ68_BLOB, _get


def _html(monkeypatch, ops=None, tage=None, standard='EASA'):
    monkeypatch.setattr(backend, '_flight_ops_load', lambda _t: dict(ops or {}))
    monkeypatch.setattr(backend, '_roster_snapshot_read',
                        lambda _t: {'tage': list(tage or [])})
    return backend._build_logbook_html(TOKEN, standard)


def test_html_zeigt_block_landungen_und_summenzeile(monkeypatch):
    _seed()
    _seed_import(BLZ68_BLOB)
    with backend.app.test_request_context(json={
            'date': '2026-05-01', 'flight': 'LH400', 'from': 'FRA',
            'to': 'JFK', 'ldg_day': 1, 'ldg_night': 2, 'to_day': 1}):
        assert backend.save_logbook_leg(TOKEN).get_json()['ok']
    r = _get()
    html = _html(monkeypatch)
    # Spalten, die die alte Fassung gar nicht hatte
    for col in ('Block', 'Ldg T', 'Ldg N', 'Start T', 'Nacht', 'Kennz.'):
        assert f'<th>{col}</th>' in html
    # Legs stehen drin, inklusive Alt-Import
    assert 'LH400' in html and 'LH620' in html and 'D-AIHY' in html
    # Blockzeit = Ist-Differenz, nicht die BLZ68-Durchschnittszahl
    assert '<td>4:12</td>' in html          # LH620 252 min
    assert '<td>3:55</td>' not in html      # 235 min BLZ68
    # Landungen aus dem Overlay sind sichtbar …
    assert '<td>1</td>' in html and '<td>2</td>' in html
    # … und die Summenzeile trägt exakt die API-Totals.
    total = r['totals']['block_min']
    assert f'<td>{total // 60}:{total % 60:02d}</td>' in html
    assert 'class="sum"' in html


def test_kein_erfundenes_sby_act_ohne_standby_beleg(monkeypatch):
    """WURZEL des Fehlers: `klass` fehlt bei jedem AeroX-Crew-Tag (die
    Tax-Pipeline läuft für sie nie) — die alte Zeile las das fehlende Feld
    als Beleg für „Standby aktiviert"."""
    _seed()
    ops = {
        # Tag MIT Flugbuch-Leg: FlightOps hängt sich an den passenden Leg
        '2026-05-01': {'flightNumber': 'LH400', 'pax_adults': 200,
                       'remarks': 'Katering komplett'},
        # Tag OHNE Flugbuch-Leg, Roster-Tag ohne Steuer-Klasse
        '2026-07-07': {'flightNumber': 'LH999', 'aircraftReg': 'D-AIMA'},
    }
    tage = [{'datum': '2026-05-01', 'klass': None, 'routing': 'FRA-JFK'},
            {'datum': '2026-07-07', 'klass': None, 'routing': 'FRA-MUC'}]
    html = _html(monkeypatch, ops=ops, tage=tage)
    assert 'SBY-ACT' not in html and 'Standby' not in html
    # Nichts geht verloren: der FlightOps-Tag ohne Leg bleibt als Zeile …
    assert 'LH999' in html and 'D-AIMA' in html
    # … und die FlightOps-Angaben landen am richtigen Leg.
    assert '<td>200</td>' in html and 'Katering komplett' in html


def test_standby_nur_mit_beleg_im_tag(monkeypatch):
    _seed()
    ops = {'2026-07-08': {'flightNumber': 'LH888'}}
    tage = [{'datum': '2026-07-08', 'klass': None, 'marker': 'SBY MUC',
             'routing': 'MUC-FCO'}]
    html = _html(monkeypatch, ops=ops, tage=tage)
    assert 'Standby aktiviert' in html


def test_pax_wird_nicht_an_den_falschen_leg_geraten(monkeypatch):
    """FlightOps liegt pro TAG. Passt die Flugnummer nicht zum Leg, wird
    nichts zugeordnet — geraten würde die PAX-Zahl am falschen Flug zeigen."""
    _seed()
    ops = {'2026-05-03': {'flightNumber': 'LH999', 'pax_adults': 111}}
    html = _html(monkeypatch, ops=ops)
    assert '<td>111</td>' not in html


def test_steuertag_ohne_leg_bleibt_erhalten(monkeypatch):
    """Additiv: Z72-Tage der Steuer-Auswertung ohne Roster-Sektor
    verschwinden nicht — sie füllen nur die Spalten, die sie belegen können.
    Ortszeit-Dienstzeiten gehören NICHT in die UTC-Spalten."""
    _seed()
    tage = [{'datum': '2026-04-02', 'klass': 'Z72', 'routing': 'FRA-LHR-FRA',
             'reader_facts': {'start_time': '06:20', 'end_time': '15:40'}}]
    html = _html(monkeypatch, tage=tage)
    assert 'FRA → LHR → FRA' in html
    assert 'Dienst 06:20–15:40 Ortszeit' in html
    assert '<td>06:20</td>' not in html


def test_leeres_logbuch_ist_ehrlich(monkeypatch):
    monkeypatch.setattr(backend, '_manual_briefings_load', lambda _t: {})
    monkeypatch.setattr(backend, '_ical_briefings_load', lambda _t: {})
    monkeypatch.setattr(backend, '_logbook_import_load', lambda _t: {})
    html = _html(monkeypatch)
    assert 'Keine Flugtage vorhanden.' in html
    assert 'colspan="16"' in html


def test_faa_variante_hat_englische_spalten(monkeypatch):
    _seed()
    html = _html(monkeypatch, standard='FAA')
    assert 'FAR 61.51' in html
    assert '<th>Ldg Day</th>' in html and '<th>Out (UTC)</th>' in html


def test_route_liefert_html(monkeypatch):
    _seed()
    monkeypatch.setattr(backend, '_flight_ops_load', lambda _t: {})
    monkeypatch.setattr(backend, '_roster_snapshot_read', lambda _t: {})
    with backend.app.test_request_context('/x?standard=EASA'):
        resp = backend.get_logbook_html(TOKEN)
    assert resp.mimetype == 'text/html'
    assert b'FCL.050' in resp.get_data()
