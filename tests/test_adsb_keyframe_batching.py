"""Regressionen für den gebatchten ADS-B-Keyframe-Lookup.

Kein echter Netz-/DB-Zugriff. Der Fake bildet nur die verwendete Supabase-
Query-Chain ab und zählt Batch- gegenüber Legacy-Einzelabfragen.
"""
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app  # noqa: F401 — Blueprint-Registrierung vor Direkt-Import
import blueprints.adsb_blueprint as ADSB


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, owner):
        self.owner = owner
        self.mode = None
        self.values = []
        self.value = None

    def select(self, _columns):
        return self

    def in_(self, field, values):
        assert field == 'hex24'
        self.mode = 'batch'
        self.values = list(values)
        return self

    def eq(self, field, value):
        assert field == 'hex24'
        self.mode = 'single'
        self.value = value
        return self

    def limit(self, _limit):
        return self

    def execute(self):
        if self.mode == 'batch':
            self.owner.batch_calls.append(tuple(self.values))
            if self.owner.fail_batches:
                raise RuntimeError('batch unsupported')
            return _Result([
                {'hex24': h, **self.owner.keyframes[h]}
                for h in self.values if h in self.owner.keyframes
            ])
        self.owner.single_calls.append(self.value)
        row = self.owner.keyframes.get(self.value)
        return _Result([row] if row else [])


class _SB:
    def __init__(self, keyframes, *, fail_batches=False):
        self.keyframes = keyframes
        self.fail_batches = fail_batches
        self.batch_calls = []
        self.single_calls = []

    def table(self, name):
        assert name == 'aircraft_positions'
        return _Query(self)


def _keyframe(i):
    return {
        'latitude': 50.0 + i / 1000,
        'longitude': 8.0,
        'altitude_m': 1000 + i,
        'on_ground': False,
    }


def test_keyframes_werden_in_begrenzten_chunks_statt_n_plus_eins_geladen(monkeypatch):
    total = ADSB._KEYFRAME_BATCH_SIZE * 2 + 17
    rows = [{'hex24': f'{i:06x}'} for i in range(total)]
    frames = {r['hex24']: _keyframe(i) for i, r in enumerate(rows)}
    sb = _SB(frames)
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (sb, True))

    out = ADSB._attach_keyframes(rows)

    assert out is rows
    assert [len(c) for c in sb.batch_calls] == [
        ADSB._KEYFRAME_BATCH_SIZE, ADSB._KEYFRAME_BATCH_SIZE, 17]
    assert sb.single_calls == []
    assert all('_keyframe' in row for row in rows)
    assert rows[-1]['_keyframe']['altitude_m'] == 1000 + total - 1
    assert 'hex24' not in rows[-1]['_keyframe']


def test_doppelte_watch_rows_teilen_ein_keyframe_ohne_extra_query(monkeypatch):
    frame = _keyframe(7)
    rows = [{'hex24': 'abc123'}, {'hex24': 'ABC123'}, {'hex24': ''}]
    sb = _SB({'abc123': frame})
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (sb, True))

    ADSB._attach_keyframes(rows)

    assert sb.batch_calls == [('abc123',)]
    assert rows[0]['_keyframe'] == frame
    assert rows[1]['_keyframe'] == frame
    assert '_keyframe' not in rows[2]


def test_batch_fehler_faellt_auf_bisherigen_einzelpfad_zurueck(monkeypatch):
    rows = [{'hex24': 'abc123'}, {'hex24': 'def456'}]
    frames = {'abc123': _keyframe(1), 'def456': _keyframe(2)}
    sb = _SB(frames, fail_batches=True)
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (sb, True))

    ADSB._attach_keyframes(rows)

    assert sb.batch_calls == [('abc123', 'def456')]
    assert sb.single_calls == ['abc123', 'def456']
    assert [r['_keyframe'] for r in rows] == [frames['abc123'], frames['def456']]
