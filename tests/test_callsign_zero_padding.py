"""Zero-Padding-Robustheit der ICAO-Funknamen (Owner 2026-07-15, LX0042).

Symptom: eine Swiss-Flugnummer mit führender Null (LX0042) lieferte im Flug-
Detail KEINE Live-Position und KEINE Soll-/Ist-Zeiten. Wurzel: der aus der
Flugnummer ABGELEITETE Funkname war inkonsistent gepaddet — der Resolver baute
`SWR042`, die Live-Positions-Ableitung `SWR42`. FR24-gRPC speichert/sendet den
Funknamen typischerweise UN-gepaddet (SWR42) und matcht ihn EXAKT
(`fr24_grpc._norm_cs` strippt keine Nullen). Ein `SWR042`-Query verfehlte also
sowohl die Positions- als auch die Zeiten-Auflösung.

Fix: kanonische Form = gestrippt (an ALLEN Ableitungsstellen), und die Queries
probieren beide Zero-Formen (`_callsign_zero_variants`). Ohne führende Null
(LX1719 → SWR1719) bleibt alles unverändert = genau EIN Kandidat.
"""
import blueprints.aerox_data_blueprint as axd
from blueprints.aerox_data_blueprint import _callsign_zero_variants


def test_variants_padded_offers_stripped_first_then_padded():
    # SWR042 → probiere zuerst SWR42 (FR24-Form), dann SWR042.
    assert _callsign_zero_variants('SWR042') == ['SWR42', 'SWR042']


def test_variants_stripped_input_offers_only_stripped():
    # Der bereits gestrippte Funkname erzeugt genau EINEN Kandidaten
    # (keine gepaddete Variante rekonstruierbar → kein zusätzlicher I/O).
    assert _callsign_zero_variants('SWR42') == ['SWR42']


def test_variants_no_leading_zero_single_candidate():
    # LX1719 → SWR1719: keine führende Null ⇒ ein Kandidat, unverändert.
    assert _callsign_zero_variants('SWR1719') == ['SWR1719']


def test_variants_multi_zero_padding():
    # Mehrfach-Padding (SWR007) → SWR7 zuerst, dann SWR007.
    assert _callsign_zero_variants('SWR007') == ['SWR7', 'SWR007']


def test_variants_pure_zero_number_survives():
    # Reiner Null-Suffix darf nicht zu leerem Suffix kollabieren.
    assert _callsign_zero_variants('SWR000') == ['SWR0', 'SWR000']


def test_variants_alnum_suffix_untouched():
    # Alphanumerischer Suffix (DLH8UA) hat keinen rein-numerischen Teil am Ende
    # → ein einziger Kandidat, keine Null-Manipulation.
    assert _callsign_zero_variants('DLH8UA') == ['DLH8UA']


def test_variants_empty_input():
    assert _callsign_zero_variants('') == []
    assert _callsign_zero_variants(None) == []


def test_free_times_derives_stripped_callsign_and_tries_both(monkeypatch):
    """Empty-facts LX0042: der freie gRPC-Zeiten-Korridor leitet den Funknamen
    aus der Flugnummer GESTRIPPT ab (SWR42) und trifft die FR24-Form."""
    tried = []

    def fake_grpc(cs, origin, dest):
        tried.append(cs)
        # FR24 kennt nur die un-gepaddete Form.
        if cs == 'SWR42':
            return {'sched_dep': 1000, 'sched_arr': 2000, 'eta': 2100}
        return None

    # Airline-Präfix LX → ICAO SWR muss auflösbar sein.
    monkeypatch.setattr(axd, '_airline_row',
                        lambda p: {'icao': 'SWR'} if (p or '').upper() == 'LX' else {})
    monkeypatch.setattr(axd, '_grpc_times_free', fake_grpc)
    monkeypatch.setattr(axd, '_epoch_to_local_iso',
                        lambda ep, iata: ('T%s@%s' % (ep, iata)) if ep else None)
    # Airport-Rows für den Korridor-Distanzcheck sind für _grpc_times_free
    # relevant, hier aber gemockt → egal. Memo für den Test leeren.
    axd._FREE_TIMES_MEMO.clear()

    out = axd._flight_times_free_first('LX0042', '2026-07-16', 'ZRH', 'LHR',
                                       callsign=None, allow_paid=False)
    # Kanonische (gestrippte) Form wurde probiert und traf.
    assert 'SWR42' in tried
    assert out.get('sched_dep') and out.get('sched_arr')


def test_free_times_passed_padded_callsign_falls_back_to_stripped(monkeypatch):
    """Wird dem Korridor ein bereits gepaddeter Funkname (SWR042) übergeben,
    probiert er beide Formen und trifft die FR24-Form (SWR42)."""
    tried = []

    def fake_grpc(cs, origin, dest):
        tried.append(cs)
        return {'sched_dep': 1, 'sched_arr': 2} if cs == 'SWR42' else None

    monkeypatch.setattr(axd, '_grpc_times_free', fake_grpc)
    monkeypatch.setattr(axd, '_epoch_to_local_iso',
                        lambda ep, iata: ('T%s' % ep) if ep else None)
    axd._FREE_TIMES_MEMO.clear()

    out = axd._flight_times_free_first('LX0042', '2026-07-16', 'ZRH', 'LHR',
                                       callsign='SWR042', allow_paid=False)
    assert tried[0] == 'SWR42'          # gestrippt zuerst
    assert out.get('sched_dep') and out.get('sched_arr')


def test_aircraft_live_pos_callsign_retry_both_forms(monkeypatch):
    """`_aircraft_live_pos` findet die Maschine, auch wenn der Harvester die
    ANDERE Zero-Form als der übergebene Funkname gespeichert hat."""
    # NAS-Pfad überspringen, direkt Supabase-Fallback.
    monkeypatch.setattr(axd, '_nas_live_pos',
                        lambda **kw: None)

    class FakeQuery:
        def __init__(self, store):
            self.store = store
            self._col = None
            self._val = None

        def select(self, *a, **k):
            return self

        def eq(self, col, val):
            if col in ('callsign', 'flight', 'reg'):
                self._col, self._val = col, val
            return self

        def gt(self, *a, **k):
            return self

        def limit(self, *a, **k):
            return self

        def execute(self):
            rows = [r for r in self.store
                    if r.get(self._col) == self._val] if self._col else []

            class R:
                def __init__(self, data):
                    self.data = data
            return R(rows)

    # Harvester speicherte die UN-gepaddete (FR24-typische) Form SWR42.
    store = [{'callsign': 'SWR42', 'flight': None, 'reg': None,
              'reg_display': 'HB-JCA', 'lat': 47.4, 'lon': 8.5, 'track': 90,
              'gs_kt': 420, 'alt_ft': 35000, 'origin': 'ZRH', 'dest': 'LHR',
              'ac_type': 'BCS3', 'on_ground': False, 'seen_ts': '2026-07-16T10:00:00Z',
              'updated_at': '2999-01-01T00:00:00Z'}]

    class FakeSB:
        def table(self, name):
            return FakeQuery(store)

    monkeypatch.setattr(axd, '_sb', lambda: FakeSB())

    # Query mit der (alt-Resolver-)GEPADDETEN Form SWR042 — der Retry muss über
    # die gestrippte Variante (SWR42) die gespeicherte Row finden.
    pos, od, reg, ac = axd._aircraft_live_pos(callsign='SWR042')
    assert pos is not None
    assert reg == 'HB-JCA'
    assert od == ('ZRH', 'LHR')
