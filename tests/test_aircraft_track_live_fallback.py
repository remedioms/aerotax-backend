"""Live-Positionen für ALLE Airlines — Lese-Rückfall auf `aircraft_track`.

OWNER-BEFUND 10.08.2026. `/api/ax/flight-detail/DL107` lieferte `live: null`,
obwohl derselbe Flug (Reg N855NW, FRA→JFK) lückenlos alle ~16 min in der
eigenen Datenbank stand. Ursache ist keine fehlende Datenquelle, sondern eine
asymmetrische Schreib-/Lese-Seite:

  * Der NAS-Harvester schreibt JEDE FR24-Zeile ZWEIMAL — nach `aircraft_live`
    GEFILTERT auf die LH-Gruppen-/DE-Carrier-Callsign-Präfixe
    (`_DEFAULT_PREFIXES` in nas_harvester/ingest.py), nach `aircraft_track`
    UNGEFILTERT.
  * `_aircraft_live_pos` las ausschließlich `aircraft_live`.

Für jede Airline außerhalb der LH-Gruppe war der Live-Read damit ein
struktureller Miss — bei vorhandenen Daten. Prod-Messung 10.08.2026: im
35-min-Fenster lagen 939 Flugnummern NUR in `aircraft_track`. Der Rückfall
liest bei einem Miss den jüngsten Breadcrumb zur Flugnummer nach.

Diese Datei hält beides fest: dass der Rückfall trägt, UND dass er dieselben
Gates fährt wie der Hauptpfad. Ein Rückfall ohne Gates wäre schlechter als gar
keiner — er würde aus „keine Anzeige" eine FALSCHE Anzeige machen
(Cross-Date-Bindung, in diesem Projekt teuer gelernt: LH712 FRA→ICN wurde
16,6 h VOR dem Abflug als „airborne" gemeldet).

Ebenfalls festgehalten: was der Rückfall NICHT liefern kann. `aircraft_track`
hat keine Spalten `callsign`, `reg_display`, `ac_type` — diese Felder bleiben
None statt geraten (Keine-Fake-Werte-Regel).
"""
import os
import sys
import time

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as _app_beim_import  # noqa: E402,F401  (Blueprint-Registrierung)
from blueprints import aerox_data_blueprint as axd  # noqa: E402


def _app():
    """Das AKTUELL geladene app-Modul — nicht der Verweis vom Import.

    ⚠️ SUITE-FALLE in diesem Repo: `test_calculation.py` importiert `app`
    absichtlich neu. Ein beim Import festgehaltener Modulverweis zeigt danach
    auf die ALTE Instanz, während der Produktionscode zur Laufzeit über
    `sys.modules['app']` auflöst — also die NEUE. Ein Monkeypatch auf den alten
    Verweis landet dann ins Leere: solo grün, in der Suite rot. Vorbild:
    tests/test_flight_recap_crossdate.py.
    """
    return sys.modules['app']


# ── Fake-Supabase ───────────────────────────────────────────────────────────
# Spiegelt genau die Query-Kette, die der Produktionscode fährt:
#   aircraft_live : select().eq(col,val).gt('updated_at',cutoff)[.eq('dest',x)].limit(1)
#   aircraft_track: select().eq('flight',v).gt('seen_ts',cutoff)[.eq('dest',x)]
#                   .order('seen_ts',desc=True).limit(1)
# `gt` wird ECHT ausgewertet (String-Vergleich auf ISO-UTC ist hier zulässig,
# alle Testdaten tragen dasselbe '...Z'-Format) — sonst könnte das Frische-Gate
# nicht getestet werden.

class _Res:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, tabelle, store, protokoll):
        self.tabelle = tabelle
        self.store = store
        self.protokoll = protokoll
        self.eqs = {}
        self.gts = {}
        self.desc = False

    def select(self, *a, **k):
        return self

    def eq(self, col, val):
        self.eqs[col] = val
        return self

    def gt(self, col, val):
        self.gts[col] = val
        return self

    def order(self, col, desc=False):
        self.sort_col, self.desc = col, desc
        return self

    def limit(self, *a, **k):
        return self

    def execute(self):
        self.protokoll.append((self.tabelle, dict(self.eqs)))
        rows = []
        for r in self.store.get(self.tabelle, []):
            if any(r.get(c) != v for c, v in self.eqs.items()):
                continue
            if any(not (r.get(c) and str(r[c]) > str(v))
                   for c, v in self.gts.items()):
                continue
            rows.append(r)
        if self.desc:
            rows = sorted(rows, key=lambda r: r.get('seen_ts') or '', reverse=True)
        return _Res(rows[:1])


class _FakeSB:
    def __init__(self, store, protokoll):
        self.store, self.protokoll = store, protokoll

    def table(self, name):
        return _FakeQuery(name, self.store, self.protokoll)


def _iso(delta_s):
    """ISO-UTC relativ zu jetzt — die Testdaten müssen sich am echten
    Frischefenster messen, nicht an einem eingefrorenen Datum."""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + delta_s))


def _track_row(**over):
    """Ein DL107-Breadcrumb, wie ihn der Harvester ungefiltert schreibt.
    Genau die Spalten aus supabase_migrations/20260709_aircraft_track.sql —
    KEIN callsign, KEIN reg_display, KEIN ac_type."""
    r = {'reg': 'N855NW', 'seen_ts': _iso(-120), 'flight': 'DL107',
         'origin': 'FRA', 'dest': 'JFK', 'lat': 52.1, 'lon': -30.4,
         'alt_ft': 37000, 'gs_kt': 480, 'track_deg': 280, 'on_ground': False}
    r.update(over)
    return r


def _setup(monkeypatch, live=(), track=()):
    """NAS-Pfad aus, Supabase gefaked, Memo geleert (prozessweit!)."""
    monkeypatch.setattr(axd, '_nas_live_pos', lambda **kw: None)
    protokoll = []
    store = {'aircraft_live': list(live), 'aircraft_track': list(track)}
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB(store, protokoll))
    axd._TRACK_POS_MEMO.clear()
    return protokoll


# ── 1. Treffer im Hauptpfad ⇒ Rückfall wird gar nicht erst befragt ──────────

def test_treffer_in_aircraft_live_fragt_den_rueckfall_nie(monkeypatch):
    """Der Rückfall ist eine ZUGABE, kein zweiter Regelweg: liefert
    `aircraft_live` eine gültige Zeile, darf `aircraft_track` keinen einzigen
    zusätzlichen Read kosten."""
    live = [{'callsign': 'DLH400', 'flight': 'LH400', 'reg': 'DAIZZ',
             'reg_display': 'D-AIZZ', 'lat': 50.0, 'lon': 8.5, 'track': 280,
             'gs_kt': 470, 'alt_ft': 36000, 'origin': 'FRA', 'dest': 'JFK',
             'ac_type': 'B748', 'on_ground': False, 'seen_ts': _iso(-60),
             'updated_at': _iso(-60)}]
    protokoll = _setup(monkeypatch, live=live, track=[_track_row(flight='LH400')])

    pos, od, reg, ac = axd._aircraft_live_pos(flight='LH400', dep='JFK')

    assert pos is not None and pos['source'] == 'aircraft_live'
    assert (od, reg, ac) == (('FRA', 'JFK'), 'D-AIZZ', 'B748')
    assert [t for t, _ in protokoll] == ['aircraft_live'], protokoll


# ── 2. Miss im Hauptpfad ⇒ Rückfall liefert die Position ────────────────────

def test_miss_in_aircraft_live_rueckfall_liefert_position(monkeypatch):
    """DER Owner-Fall: DL107 steht NICHT in `aircraft_live` (kein LH-Präfix),
    aber lückenlos in `aircraft_track`. Vorher `live: null` — jetzt die echte
    Position aus der eigenen Datenbank."""
    protokoll = _setup(monkeypatch, live=[], track=[_track_row()])

    pos, od, reg, ac = axd._aircraft_live_pos(flight='DL107', dep='JFK')

    assert pos is not None, 'Rückfall hätte greifen müssen'
    assert (pos['lat'], pos['lon']) == (52.1, -30.4)
    assert pos['source'] == 'aircraft_track'
    assert pos['on_ground'] is False        # 37000 ft / 480 kt ⇒ airborne
    assert pos['track'] == 280              # Spalte heißt dort `track_deg`
    assert od == ('FRA', 'JFK')
    assert reg is None                      # keine reg_display-Spalte ⇒ leer
    assert ac is None                       # keine ac_type-Spalte ⇒ nicht raten
    assert [t for t, _ in protokoll] == ['aircraft_live', 'aircraft_track']


def test_rueckfall_auch_ohne_dep_constraint(monkeypatch):
    """Ohne `dep` entfällt das Route-Gate (wie im Hauptpfad) — die Position
    kommt trotzdem, die Route wird ehrlich mitgeliefert."""
    _setup(monkeypatch, live=[], track=[_track_row()])
    pos, od, _reg, _ac = axd._aircraft_live_pos(flight='DL107')
    assert pos is not None and od == ('FRA', 'JFK')


def test_rueckfall_findet_auch_die_icao_schreibweise(monkeypatch):
    """`aircraft_track.flight` trägt die IATA- ODER die ICAO-Schreibweise (im
    Repo belegt: app._route_entry_track_keys; Prod-Stichprobe 10.08.2026 hatte
    `AC847` UND `ACA847`). Es gibt dort keine eigene `callsign`-Spalte — der
    Funkname wird deshalb gegen dieselbe Spalte probiert, aber erst NACH allen
    Flugnummer-Kandidaten."""
    protokoll = _setup(monkeypatch, live=[],
                       track=[_track_row(flight='ACA847', reg='CFITW')])

    pos, od, _reg, _ac = axd._aircraft_live_pos(flight='AC847', callsign='ACA847',
                                                dep='JFK')
    assert pos is not None and od == ('FRA', 'JFK')
    # Flugnummer zuerst, Funkname als Nachzügler.
    gefragt = [e.get('flight') for t, e in protokoll if t == 'aircraft_track']
    assert gefragt == ['AC847', 'ACA847']


def test_funkname_gleich_flugnummer_kostet_keinen_zweiten_read(monkeypatch):
    """Duplikate werden entfernt — ein Funkname, der auf dieselbe Zeichenkette
    fällt, darf keinen zusätzlichen Read auslösen."""
    protokoll = _setup(monkeypatch, live=[], track=[])
    axd._aircraft_live_pos(flight='DL107', callsign='DL107', dep='JFK')
    gefragt = [e.get('flight') for t, e in protokoll if t == 'aircraft_track']
    assert gefragt == ['DL107']


def test_rueckfall_ist_zero_padding_robust(monkeypatch):
    """Wie der Hauptpfad: das Roster liefert DL0107, der Harvester schreibt die
    rohe FR24-Nummer DL107."""
    _setup(monkeypatch, live=[], track=[_track_row(flight='DL107')])
    pos, _od, _reg, _ac = axd._aircraft_live_pos(flight='DL0107', dep='JFK')
    assert pos is not None


# ── 3. Frischefenster ⇒ zu alter Breadcrumb liefert KEINE Position ──────────

def test_zu_alter_breadcrumb_liefert_keine_position(monkeypatch):
    """`aircraft_track` hat 60 Tage Retention — ohne Frische-Gate wäre der
    Rückfall ein Archiv-Leser, der alte Spuren als „live" ausgibt. Gleiche
    Konstante wie der Hauptpfad (max_age_min, Default 35)."""
    _setup(monkeypatch, live=[], track=[_track_row(seen_ts=_iso(-40 * 60))])
    pos, od, reg, ac = axd._aircraft_live_pos(flight='DL107', dep='JFK')
    assert (pos, od, reg, ac) == (None, None, None, None)


def test_frischefenster_folgt_dem_uebergebenen_max_age(monkeypatch):
    """Kein eigenes Fenster: derselbe Breadcrumb, der bei 35 min durchfällt,
    kommt bei einem großzügigeren `max_age_min` des Aufrufers durch."""
    _setup(monkeypatch, live=[], track=[_track_row(seen_ts=_iso(-40 * 60))])
    pos, _od, _reg, _ac = axd._aircraft_live_pos(flight='DL107', dep='JFK',
                                                 max_age_min=60)
    assert pos is not None


# ── 4. Route- und Instanz-Gate ⇒ KEINE Position ─────────────────────────────

def test_falsche_route_liefert_keine_position(monkeypatch):
    """Route-Gate wie im Bestandspfad: der Breadcrumb muss NACH `dep` fliegen
    (dest == dep). Eine gleich nummerierte Maschine auf dem Rückweg ist ein
    anderer Leg und darf nicht durchgehen."""
    _setup(monkeypatch, live=[], track=[_track_row(origin='JFK', dest='FRA')])
    pos, od, reg, ac = axd._aircraft_live_pos(flight='DL107', dep='JFK')
    assert (pos, od, reg, ac) == (None, None, None, None)


def test_falsche_instanz_gestern_liefert_keine_position(monkeypatch):
    """CROSS-DATE-GATE (die teuer gelernte Fehlerklasse). Der Breadcrumb ist
    FRISCH (2 min alt) und route-konsistent — aber der Leg, für den gefragt
    wird, startet erst in 18 h. Dann fliegt hier beweisbar die Instanz von
    GESTERN. Ohne dieses Gate stünde ein noch gar nicht gestarteter Flug als
    „airborne" in der App."""
    _setup(monkeypatch, live=[], track=[_track_row()])
    pos, od, reg, ac = axd._aircraft_live_pos(
        flight='DL107', dep='JFK', sched_dep_iso=_iso(18 * 3600))
    assert (pos, od, reg, ac) == (None, None, None, None)


def test_richtige_instanz_kommt_durch(monkeypatch):
    """Gegenprobe zum Cross-Date-Gate: derselbe Breadcrumb im Instanzfenster
    [dep−6 h, dep+20 h] wird NICHT verworfen."""
    _setup(monkeypatch, live=[], track=[_track_row()])
    pos, _od, _reg, _ac = axd._aircraft_live_pos(
        flight='DL107', dep='JFK', sched_dep_iso=_iso(-2 * 3600))
    assert pos is not None


def test_taxi_snapshot_wird_als_on_ground_gewertet(monkeypatch):
    """Taxi-Gate wie im Bestandspfad — `aircraft_track` führt alt_ft/gs_kt/
    on_ground, das Gate greift also vollständig. Ein Pushback-Snapshot
    (on_ground=false, ~15 kt, keine Höhe) darf nicht als Flugposition gelten,
    sonst extrapoliert die App einen kriechenden Geister-Flieger."""
    _setup(monkeypatch, live=[],
           track=[_track_row(alt_ft=None, gs_kt=15, on_ground=False)])
    pos, _od, _reg, _ac = axd._aircraft_live_pos(flight='DL107', dep='JFK')
    assert pos is not None and pos['on_ground'] is True


# ── 5. Fehlende Felder bleiben None statt geraten ───────────────────────────

def test_fehlende_spalten_bleiben_none_statt_geraten(monkeypatch):
    """EHRLICHE GRENZE des Rückfalls. `aircraft_track` hat KEINE Spalten
    `callsign`, `reg_display`, `ac_type`. Der Funkname ließe sich NICHT aus der
    Flugnummer ableiten (LH1131 = DLH08F ist alphanumerisch) — ein geratener
    Wert würde iOS bei adsb.lol ins Leere pollen lassen. Also: leer statt Fake.
    """
    _setup(monkeypatch, live=[], track=[_track_row()])
    pos, _od, reg, ac = axd._aircraft_live_pos(flight='DL107', dep='JFK')

    assert pos['callsign'] is None
    assert ac is None
    # Die Reg steht in `aircraft_track` NUR normalisiert (Teil des
    # Primärschlüssels). Sie als Anzeigewert zurückzugeben wäre AKTIV schädlich:
    # `crew_live_state` macht `reg = live_reg or reg` und würde den korrekten
    # Roster-Tail `F-GSPA` durch `FGSPA` ERSETZEN. Wo der Bindestrich hingehört,
    # ist aus der normalisierten Form nicht ableitbar ⇒ lieber kein Kennzeichen.
    assert reg is None


def test_fehlendes_origin_bleibt_none(monkeypatch):
    """Alte Breadcrumbs können ohne `origin` liegen. Dann bleibt der
    Abflughafen leer — er wird NICHT aus dem Roster hineingeschrieben."""
    _setup(monkeypatch, live=[], track=[_track_row(origin=None)])
    pos, od, _reg, _ac = axd._aircraft_live_pos(flight='DL107', dep='JFK')
    assert pos is not None and od == (None, 'JFK')


def test_breadcrumb_ohne_koordinaten_liefert_nichts(monkeypatch):
    _setup(monkeypatch, live=[], track=[_track_row(lat=None, lon=None)])
    pos, od, reg, ac = axd._aircraft_live_pos(flight='DL107', dep='JFK')
    assert (pos, od, reg, ac) == (None, None, None, None)


# ── Performance: der Miss darf nicht pro Poll einen Read kosten ─────────────

def test_miss_wird_memoisiert_kein_read_pro_poll(monkeypatch):
    """Bei fremden Airlines ist der Miss der DAUERZUSTAND (Flug am Boden, Flug
    außerhalb des Sweeps). Ohne Memo zöge jeder iOS-Poll einen zusätzlichen
    Supabase-Read über eine ~19,4-Mio-Zeilen-Tabelle nach sich."""
    protokoll = _setup(monkeypatch, live=[], track=[])

    for _ in range(3):
        assert axd._aircraft_live_pos(flight='DL107', dep='JFK')[0] is None

    treffer = [t for t, _ in protokoll if t == 'aircraft_track']
    assert len(treffer) == 1, protokoll


def test_treffer_wird_memoisiert(monkeypatch):
    protokoll = _setup(monkeypatch, live=[], track=[_track_row()])

    for _ in range(3):
        assert axd._aircraft_live_pos(flight='DL107', dep='JFK')[0] is not None

    treffer = [t for t, _ in protokoll if t == 'aircraft_track']
    assert len(treffer) == 1, protokoll


def test_memo_gatet_die_instanz_trotzdem_pro_aufrufer(monkeypatch):
    """Memoisiert wird die ROHZEILE, nicht das Ergebnis: das Instanz-Gate hängt
    am `sched_dep_iso` des Aufrufers und muss auch beim Cache-Hit greifen —
    sonst würde ein Aufrufer die Gate-Entscheidung eines anderen erben."""
    _setup(monkeypatch, live=[], track=[_track_row()])

    assert axd._aircraft_live_pos(flight='DL107', dep='JFK',
                                  sched_dep_iso=_iso(-2 * 3600))[0] is not None
    # gleicher Memo-Key, anderer Leg → muss verworfen werden
    assert axd._aircraft_live_pos(flight='DL107', dep='JFK',
                                  sched_dep_iso=_iso(18 * 3600))[0] is None


# ── Robustheit: der Rückfall darf den Hauptpfad nie zum Fehler bringen ──────

def test_defekter_store_wirft_nicht(monkeypatch):
    """Ein Supabase-Client ohne `order()` (oder ein Netzfehler) darf nicht als
    Exception aus `_aircraft_live_pos` fallen — der Rückfall ist best-effort."""
    monkeypatch.setattr(axd, '_nas_live_pos', lambda **kw: None)

    class _Kaputt:
        def table(self, name):
            return self

        def select(self, *a, **k):
            return self

        def eq(self, *a, **k):
            return self

        def gt(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            return _Res([])

    monkeypatch.setattr(axd, '_sb', lambda: _Kaputt())
    axd._TRACK_POS_MEMO.clear()
    assert axd._aircraft_live_pos(flight='DL107', dep='JFK') == \
        (None, None, None, None)


def test_ohne_flugnummer_kein_zusaetzlicher_read(monkeypatch):
    """Der Rückfall geht ÜBER die Flugnummer (Index idx_aircraft_track_flt_ts).
    Ohne Flugnummer (reiner Reg-Lookup, z.B. Ferry) gibt es nichts zu fragen —
    und keinen Read."""
    protokoll = _setup(monkeypatch, live=[], track=[_track_row()])
    assert axd._aircraft_live_pos(reg='N855NW', dep='JFK') == \
        (None, None, None, None)
    assert [t for t, _ in protokoll if t == 'aircraft_track'] == []


def test_app_modul_wird_spaet_aufgeloest():
    """Wächter für die Suite-Falle oben: `_app()` liefert das aktuell geladene
    Modul, nicht den beim Import festgehaltenen Verweis."""
    assert _app() is sys.modules['app']
