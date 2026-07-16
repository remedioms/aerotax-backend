"""PATHOLOGIE-MUSEUM — echte kaputte Board-Datensätze als Fixtures eingefroren.

Idee (Owner/Fable 2026-07-16): jeder reale Prod-Vorfall an der Board-
Normalisierungs-/Resolver-Schicht wird als „Museumsstück" (JSON in
tests/pathologie/) eingefroren. Ein Contract-Test garantiert, dass die ECHTEN
Funktionen (_flight_facts_from_obs / _flight_obs_merged / resolve_crew_live_state)
aus JEDEM Stück einen Datensatz mit harten Invarianten machen. So kann kein
einmal gefixter Fall je unbemerkt zurückkommen — und ein neuer Vorfall =
eine neue JSON-Datei (+ ggf. 3 Zeilen Spezial-Asserts hier).

Diese Datei fasst KEINEN bestehenden Code an; sie liest nur die echten
Funktionen (App/Blueprints) und die Fixtures. Der Mock-Stil (_FakeSB/_FakeQ,
_departed_rows_from_store, obs_lookup-Callable) spiegelt exakt die bestehenden
Suiten test_flight_facts_from_obs / test_leg_delay_enrichment / test_crew_live_state.

Museums-Inventar (Stand 2026-07-16, echte Daten 14./15.07.):
  • LH867  — zwei Soll-Zeiten + nackter Repoll (Folgetags-Kontamination)
  • LH890  — dep-only, Ziel nie geharvestet (altert zu landed)
  • LH423  — Text-Delay im Status + naive Soll-Zeit (Materialisierung + Pinning)
  • LH1126 — Scraper-Müll-Status ('Boarding' auf ARR-Row)
  • Übernacht-Legitim — Anti-False-Positive (d+1-Ankunft NICHT scrubben)

GENERISCHE Invarianten (für JEDES Stück geprüft):
  (a) jede est_*-Zeit im Output trägt expliziten UTC/Offset (kein naiver ISO),
  (b) delay_min ist int oder None (nie String),
  (c) est_arr eines Legs liegt nie > 2 h VOR dessen est_dep,
  (d) kein 'Ist'-Feld auf einem Leg, dessen Soll-Abflug in der Zukunft liegt.
"""
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import glob
import json
import types
from datetime import datetime, timezone

import pytest
from unittest.mock import patch, MagicMock

import app as A
import blueprints.aerox_data_blueprint as axd
from blueprints.crew_live_state import resolve_crew_live_state


# ── Fixture-Laden ────────────────────────────────────────────────────────────
_MUSEUM_DIR = os.path.join(os.path.dirname(__file__), 'pathologie')


def _load(name):
    with open(os.path.join(_MUSEUM_DIR, name), encoding='utf-8') as fh:
        return json.load(fh)


def _all_fixtures():
    return sorted(glob.glob(os.path.join(_MUSEUM_DIR, '*.json')))


def _iter_ids():
    for path in _all_fixtures():
        yield os.path.basename(path), _load(path)


# ── Mocks (identisch zum Stil der bestehenden Suiten) ────────────────────────
class _FakeQ:
    def __init__(self, rows):
        self._rows = rows

    def __getattr__(self, name):
        def _chain(*a, **kw):
            return self
        return _chain

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _FakeSB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQ(self._rows)


def _store_from(mapping):
    def _fn(key):
        return list(mapping.get(key, []))
    return _fn


def _piso(s):
    """ISO mit optionalem 'Z' → aware datetime (fromisoformat frisst kein 'Z')."""
    return datetime.fromisoformat(str(s).replace('Z', '+00:00'))


# ── Facts-/Merge-Auswertung eines Museumsstücks ──────────────────────────────
def _facts_for(spec, monkeypatch):
    """Ruft die ECHTE _flight_facts_from_obs mit den eingefrorenen Rows."""
    monkeypatch.setattr(axd, '_sb', lambda: _FakeSB(spec['rows']))
    monkeypatch.setattr(axd, '_tail_active_guard', lambda r: True)
    q = spec['query']
    return axd._flight_facts_from_obs(q['flight'], q.get('date'),
                                      q.get('dep_iata'), q.get('arr_iata'))


def _merged_for(spec):
    """Ruft die ECHTE _flight_obs_merged mit dem eingefrorenen Store (free-only,
    KEIN paid Board — _flight_from_live_board wirft, falls es je erreicht wird)."""
    A._FLIGHT_MERGE_CACHE.clear()
    q = spec['query']
    with patch.object(A, '_flight_from_free_board', return_value=None), \
            patch.object(A, '_flight_from_live_board',
                         MagicMock(side_effect=AssertionError('paid board!'))), \
            patch.object(A, '_departed_rows_from_store',
                         side_effect=_store_from(spec['store'])):
        return A._flight_obs_merged(q['flight'], date=None,
                                    dep_iata=q.get('dep_iata'),
                                    arr_iata=q.get('arr_iata'),
                                    live=True, free_only=True)


# ══════════════════════════════════════════════════════════════════════════════
# GENERISCHE Invarianten — für JEDES Museumsstück, an den REALEN Funktionen.
# ══════════════════════════════════════════════════════════════════════════════
def _output_dicts(spec, monkeypatch):
    """Alle resolvten Datensätze eines Stücks (facts- und/oder merged-Shape),
    über die die generischen Invarianten laufen."""
    outs = []
    if 'rows' in spec:
        f = _facts_for(spec, monkeypatch)
        if f:
            outs.append(('facts', f))
    if 'store' in spec:
        m = _merged_for(spec)
        if m:
            outs.append(('merged', m))
    return outs


# Feldnamen der beiden Shapes (facts vs merged), die eine ABSOLUTE Ist-/Soll-Zeit
# tragen — nur diese müssen einen Offset haben (bare 'HH:MM' ist absichtlich roh).
_TIME_FIELDS = ('est_dep', 'est_arr', 'sched_dep', 'sched_arr',
                'est_dep_iso', 'est_arr_iso', 'esti_dep', 'esti_arr')
_DELAY_FIELDS = ('delay_min', 'dep_delay_min', 'arr_delay_min')


def _has_explicit_offset(val):
    """True, wenn `val` ein ISO-Datetime mit explizitem UTC/Offset ist."""
    if not isinstance(val, str) or 'T' not in val:
        return True                 # bare 'HH:MM'/leer = kein absoluter ISO → ok
    try:
        dt = _piso(val)
    except Exception:
        return True                 # unparsbar → hier nicht als naiv werten
    return dt.tzinfo is not None


@pytest.mark.parametrize('name,spec', list(_iter_ids()))
def test_generic_no_naive_absolute_iso(name, spec, monkeypatch):
    """(a) Jede ABSOLUTE Zeit im Output trägt expliziten UTC/Offset — nie naiv."""
    for shape, out in _output_dicts(spec, monkeypatch):
        for k in _TIME_FIELDS:
            v = out.get(k)
            assert _has_explicit_offset(v), \
                f'{name} [{shape}] {k}={v!r} ist ein naiver ISO (kein Offset)'


@pytest.mark.parametrize('name,spec', list(_iter_ids()))
def test_generic_delay_min_int_or_none(name, spec, monkeypatch):
    """(b) delay_min-Felder sind int oder None — nie String/Float-Text."""
    for shape, out in _output_dicts(spec, monkeypatch):
        for k in _DELAY_FIELDS:
            v = out.get(k)
            assert v is None or isinstance(v, int), \
                f'{name} [{shape}] {k}={v!r} ist kein int/None'


@pytest.mark.parametrize('name,spec', list(_iter_ids()))
def test_generic_est_arr_not_before_est_dep(name, spec, monkeypatch):
    """(c) est_arr liegt nie > 2 h VOR est_dep desselben Legs."""
    for shape, out in _output_dicts(spec, monkeypatch):
        ed = out.get('est_dep') or out.get('est_dep_iso') or out.get('esti_dep')
        ea = out.get('est_arr') or out.get('est_arr_iso') or out.get('esti_arr')
        if not (isinstance(ed, str) and isinstance(ea, str)
                and 'T' in ed and 'T' in ea):
            continue
        try:
            d_dt, a_dt = _piso(ed), _piso(ea)
        except Exception:
            continue
        if d_dt.tzinfo is None or a_dt.tzinfo is None:
            continue
        gap_h = (a_dt - d_dt).total_seconds() / 3600.0
        assert gap_h >= -2.0, \
            f'{name} [{shape}] est_arr {ea} liegt {-gap_h:.1f}h VOR est_dep {ed}'


@pytest.mark.parametrize('name,spec', list(_iter_ids()))
def test_generic_no_ist_field_on_future_departure(name, spec, monkeypatch):
    """(d) Kein 'Ist'-Feld (est_dep/est_arr) auf einem Leg, dessen Soll-Abflug
    in der ferner Zukunft liegt (> 2 h nach dem Beobachtungshorizont des Stücks).
    Als „jetzt"-Anker dient der jüngste updated_at der Rows bzw. das Query-Datum;
    fehlt beides, wird die Invariante fail-open übersprungen."""
    for shape, out in _output_dicts(spec, monkeypatch):
        sd = out.get('sched_dep')
        if not (isinstance(sd, str) and 'T' in sd):
            continue
        try:
            sd_dt = _piso(sd)
        except Exception:
            continue
        if sd_dt.tzinfo is None:
            continue
        # Beobachtungs-Anker: jüngster updated_at (Rows) — das Stück ist ein
        # eingefrorener Zeitpunkt, keine Zukunfts-Simulation.
        anchor = None
        for r in spec.get('rows', []):
            ua = r.get('updated_at')
            if ua:
                try:
                    u = _piso(ua)
                    if anchor is None or u > anchor:
                        anchor = u
                except Exception:
                    pass
        if anchor is None:
            continue
        # Soll-Abflug NICHT in der fernen Zukunft relativ zum Beobachtungs-Anker:
        # sonst dürfte gar keine Ist-Zeit existieren.
        if sd_dt > anchor:
            for k in ('est_dep', 'est_arr', 'est_dep_iso', 'est_arr_iso',
                      'esti_dep', 'esti_arr'):
                assert not out.get(k), \
                    (f'{name} [{shape}] {k}={out.get(k)!r} auf einem Leg mit '
                     f'Zukunfts-Soll-Abflug ({sd} > Beobachtung {anchor.isoformat()})')


# ══════════════════════════════════════════════════════════════════════════════
# STÜCK-SPEZIFISCHE Invarianten — je Museumsstück die harten Erwartungen.
# Ein neues Stück braucht i.d.R. nur eine neue JSON-Datei; die generischen Blöcke
# oben decken es automatisch mit ab. Die folgenden Blöcke prüfen zusätzlich die
# im Fixture deklarierten `facts_*`/`crew_state`/`status_category`-Erwartungen —
# datengetrieben, sodass neue Stücke ohne neuen Code mitgeprüft werden.
# ══════════════════════════════════════════════════════════════════════════════
_ROWS_FIXTURES = [(os.path.basename(p), _load(p)) for p in _all_fixtures()
                  if 'rows' in _load(p)]


@pytest.mark.parametrize('name,spec', _ROWS_FIXTURES)
def test_facts_invariants(name, spec, monkeypatch):
    """Deklarierte harte facts-Werte + An-/Abwesenheit + Status-Substrings."""
    f = _facts_for(spec, monkeypatch)
    assert f, f'{name}: _flight_facts_from_obs lieferte nichts'
    for k, want in spec.get('facts_invarianten', {}).items():
        assert f.get(k) == want, f'{name}: {k}={f.get(k)!r} != {want!r}'
    for k in spec.get('facts_abwesend', []):
        assert not f.get(k), f'{name}: {k}={f.get(k)!r} sollte abwesend/leer sein'
    for k, subs in spec.get('facts_status_enthaelt', {}).items():
        val = (f.get(k) or '').lower()
        assert any(s in val for s in subs), \
            f'{name}: {k}={f.get(k)!r} enthält keins von {subs}'
    for k, subs in spec.get('facts_status_nicht', {}).items():
        val = (f.get(k) or '').lower()
        assert not any(s in val for s in subs), \
            f'{name}: {k}={f.get(k)!r} enthält verbotenes {subs}'
    if spec.get('facts_nicht_gescrubbt'):
        assert not f.get('esti_scrubbed'), \
            f'{name}: Datensatz wurde fälschlich gescrubbt (Anti-False-Positive)'


def _resolve_crew(cs, now_iso):
    obs = cs.get('obs', {})
    return resolve_crew_live_state(
        cs['sectors'],
        lambda fno, frm, to: obs.get(fno),
        lambda fno, frm, to: None,
        _piso(now_iso),
        homebase=cs.get('homebase'))


_CREW_CASES = []
for _p in _all_fixtures():
    _s = _load(_p)
    _cs = _s.get('crew_state')
    if not _cs:
        continue
    for _i, _case in enumerate(_cs.get('faelle', [])):
        _CREW_CASES.append((os.path.basename(_p), _cs, _case, _i))


@pytest.mark.parametrize('name,cs,case,idx',
                         _CREW_CASES,
                         ids=[f'{c[0]}#{c[3]}' for c in _CREW_CASES])
def test_crew_state_invariants(name, cs, case, idx):
    """Der resolvte Crew-Live-State pro Zeitpunkt. Offene Fälle (`offen: true`)
    sind als xfail(strict=False) markiert — sie dokumentieren eine am ECHTEN Code
    (nicht in diesem Test!) noch offene Invariante, ohne die Suite rot zu färben."""
    if case.get('offen'):
        pytest.xfail(case.get('reason', 'offen'))
    r = _resolve_crew(cs, case['now_iso'])
    assert r['state'] == case['erwartet_state'], \
        (f'{name} @ {case["now_iso"]}: state={r["state"]!r} != '
         f'{case["erwartet_state"]!r}')
    # Generische crew_state-Invariante (kein „Ankunft vor Abflug"): fliegt/gelandet
    # dürfen die Ankunft nie VOR dem Abflug behaupten.
    leg = r.get('current_leg') or {}
    dep_iso, arr_iso = leg.get('dep_iso'), leg.get('arr_iso')
    if dep_iso and arr_iso:
        try:
            assert _piso(arr_iso) >= _piso(dep_iso), \
                f'{name}: Leg-Ankunft {arr_iso} liegt vor Abflug {dep_iso}'
        except (ValueError, TypeError):
            pass


_STATUS_CASES = []
for _p in _all_fixtures():
    _s = _load(_p)
    if 'status_category' in _s and 'rows' in _s:
        _STATUS_CASES.append((os.path.basename(_p), _s))


@pytest.mark.parametrize('name,spec', _STATUS_CASES,
                         ids=[c[0] for c in _STATUS_CASES])
def test_status_category_invariant(name, spec, monkeypatch):
    """status_category aus den geteilten Fakten. Offene Fälle sind xfail-markiert
    (die Ableitung ist am ECHTEN Code noch nicht gelöst; s. `reason`)."""
    sc = spec['status_category']
    if sc.get('offen'):
        pytest.xfail(sc.get('reason', 'offen'))
    f = _facts_for(spec, monkeypatch)
    q = spec['query']
    now = int(_piso(sc['now_iso']).timestamp())
    flight = {'flight': q['flight'], 'dep_iata': q.get('dep_iata'),
              'arr_iata': q.get('arr_iata')}
    got = axd._status_category_from_facts(flight, f, now=now)
    assert got == sc['erwartet'], f'{name}: status_category={got!r} != {sc["erwartet"]!r}'


# ══════════════════════════════════════════════════════════════════════════════
# LH423 — Text-Delay-Materialisierung an der Merge-Grenze (nur wenn der
# Parallel-Agent sie materialisiert; sonst tolerant, als separater Test).
# ══════════════════════════════════════════════════════════════════════════════
def test_lh423_text_delay_materialized_if_present():
    """LH423 'Delayed 75 Minutes' (nur Text, naives Soll): WENN der Merge den
    Delay in harte Felder materialisiert hat, MÜSSEN sie stimmen (75 / Soll+75 /
    delay_side='dep'). Fehlt das Feld (nicht materialisiert), wird der Fall
    tolerant übersprungen — kein xfail, sondern datengetriebene Laufzeit-Prüfung."""
    spec = _load('lh423_text_delay_naive.json')
    m = _merged_for(spec)
    assert m is not None
    inv = spec['merged_optional_invarianten']
    if m.get('dep_delay_min') is None:
        pytest.skip('Text-Delay nicht materialisiert (Feld ungesetzt) — toleriert')
    assert m['dep_delay_min'] == inv['dep_delay_min']
    assert m['delay_side'] == inv['delay_side']
    assert m['esti_dep'] == inv['esti_dep']
    # Konsistenz: est_dep_iso (abgeleitet) trägt Offset/UTC.
    if m.get('est_dep_iso'):
        assert _has_explicit_offset(m['est_dep_iso'])


# ══════════════════════════════════════════════════════════════════════════════
# LH423_ARRDAY — Übernacht-Leg: ARR-Seite vom ANKUNFTSTAG lesen (arr_date-Pfad).
# Eigener Test (nicht die generischen Blöcke): das Stück braucht ein gepinntes
# „jetzt" (_airport_local_now) sowie getrennte SB-/Store-Rows pro Datum, weil
# _flight_obs_merged für Vortage die SB-Persistenz und für heute den In-Memory-
# Store liest. Betrachtet am 16.07 nach der Landung.
# ══════════════════════════════════════════════════════════════════════════════
def _merged_arrday(spec, monkeypatch):
    """Ruft die ECHTE _flight_obs_merged mit date=Abflugtag + arr_date=Ankunftstag,
    _airport_local_now gepinnt und SB-/Store-Rows pro Datum getrennt."""
    A._FLIGHT_MERGE_CACHE.clear()
    now = _piso(spec['airport_now_iso'])
    sb_rows = spec.get('sb_rows', {})
    store_rows = spec.get('store_rows', {})

    def _fake_local_now(iata):
        return now

    def _fake_sb_for_date(date_str, airport='FRA', dest_iata=None):
        d = str(date_str)[:10]
        ap = (airport or '').upper()
        return list(sb_rows.get(f'{ap}|{d}', []))

    def _fake_store(key):
        return list(store_rows.get(key, []))

    q = spec['query']
    with patch.object(A, '_airport_local_now', side_effect=_fake_local_now), \
            patch.object(A, '_flight_from_free_board', return_value=None), \
            patch.object(A, '_flight_from_live_board',
                         MagicMock(side_effect=AssertionError('paid board!'))), \
            patch.object(A, '_delay_obs_rows_for_date',
                         side_effect=_fake_sb_for_date), \
            patch.object(A, '_departed_rows_from_store', side_effect=_fake_store):
        return A._flight_obs_merged(
            q['flight'], date=spec['query_dep_date'],
            dep_iata=q.get('dep_iata'), arr_iata=q.get('arr_iata'),
            live=True, free_only=True, arr_date=spec.get('query_arr_date'))


def _merged_depday(spec):
    """Wie _merged_arrday, aber OHNE arr_date (altes Verhalten): die date-15-
    'Geplant'-Kontamination darf NIE eine Ist-Ankunft ans Leg hängen."""
    A._FLIGHT_MERGE_CACHE.clear()
    now = _piso(spec['airport_now_iso'])
    sb_rows = spec.get('sb_rows', {})
    store_rows = spec.get('store_rows', {})
    q = spec['query']
    with patch.object(A, '_airport_local_now', side_effect=lambda iata: now), \
            patch.object(A, '_flight_from_free_board', return_value=None), \
            patch.object(A, '_flight_from_live_board',
                         MagicMock(side_effect=AssertionError('paid board!'))), \
            patch.object(A, '_delay_obs_rows_for_date',
                         side_effect=lambda ds, airport='FRA', dest_iata=None:
                         list(sb_rows.get(f'{(airport or "").upper()}|{str(ds)[:10]}', []))), \
            patch.object(A, '_departed_rows_from_store',
                         side_effect=lambda key: list(store_rows.get(key, []))):
        return A._flight_obs_merged(
            q['flight'], date=spec['query_dep_date'],
            dep_iata=q.get('dep_iata'), arr_iata=q.get('arr_iata'),
            live=True, free_only=True)


def test_lh423_arrday_landing_reads_arrival_day(monkeypatch):
    """Übernacht-Leg + arr-Tag-Landung ⇒ landed-Status + est_arr/-delay vom
    Ankunftstag (16.07 08:16 'baggage delivery', +86), NICHT die date-15-'Geplant'-
    Kontamination der gestrigen Rotation."""
    spec = _load('lh423_arrday_landing.json')
    m = _merged_arrday(spec, monkeypatch)
    assert m is not None
    inv = spec['merged_arrday_invarianten']
    for k, want in inv.items():
        assert m.get(k) == want, f'LH423_ARRDAY: {k}={m.get(k)!r} != {want!r}'
    # Status ist ein Landed-Bucket-Token (iOS DelayLogic.phase → 'gelandet').
    val = (m.get('status') or '').lower()
    assert any(s in val for s in spec['merged_arrday_status_enthaelt']), \
        f'LH423_ARRDAY: status={m.get("status")!r} ist kein Landed-Bucket'
    from app import _flight_status_bucket
    assert _flight_status_bucket(m.get('status')) == 'landed'
    # est_arr trägt expliziten UTC-Offset (keine naive Zeit).
    assert _has_explicit_offset(m.get('est_arr_iso'))


def test_lh423_depday_only_drops_contaminant_arr(monkeypatch):
    """OHNE arr_date (nur Abflugtag): die date-15-'Geplant'-Kontamination darf
    KEINE Ist-Ankunft ans Leg hängen — der DAY-BOUNDARY-GUARD verwirft die falsche
    Instanz, die echte Landung (date-16) wird ohne arr_date nicht gefunden. So ist
    belegt, dass ERST arr_date die richtige Ankunft bringt (kein stiller Zufall)."""
    spec = _load('lh423_arrday_landing.json')
    if not spec.get('merged_depday_only_abwesend_est_arr'):
        pytest.skip('nicht deklariert')
    m = _merged_depday(spec)
    assert m is not None
    # Kein Ist-Ankunfts-Feld aus der falschen date-15-Instanz.
    assert not m.get('est_arr_iso'), \
        f'LH423_ARRDAY[depday]: est_arr_iso={m.get("est_arr_iso")!r} sollte leer sein'
    assert m.get('arr_delay_min') is None


def test_lh423_arrday_non_overnight_unchanged(monkeypatch):
    """NICHT-Übernacht-Regression: dieselbe Query mit arr_date == dep-date (also
    KEIN Übernacht-Fall) muss sich exakt wie ohne arr_date verhalten — die date-15-
    Row bleibt die einzige arr-Quelle, keine date-16-Werte lecken durch."""
    spec = _load('lh423_arrday_landing.json')
    now = _piso(spec['airport_now_iso'])
    sb_rows = spec.get('sb_rows', {})
    store_rows = spec.get('store_rows', {})
    q = spec['query']
    A._FLIGHT_MERGE_CACHE.clear()
    with patch.object(A, '_airport_local_now', side_effect=lambda iata: now), \
            patch.object(A, '_flight_from_free_board', return_value=None), \
            patch.object(A, '_flight_from_live_board',
                         MagicMock(side_effect=AssertionError('paid board!'))), \
            patch.object(A, '_delay_obs_rows_for_date',
                         side_effect=lambda ds, airport='FRA', dest_iata=None:
                         list(sb_rows.get(f'{(airport or "").upper()}|{str(ds)[:10]}', []))), \
            patch.object(A, '_departed_rows_from_store',
                         side_effect=lambda key: list(store_rows.get(key, []))):
        # arr_date == dep-date → im Merge auf None normalisiert (kein Übernacht).
        m_same = A._flight_obs_merged(
            q['flight'], date=spec['query_dep_date'], dep_iata=q.get('dep_iata'),
            arr_iata=q.get('arr_iata'), live=True, free_only=True,
            arr_date=spec['query_dep_date'])
    m_none = _merged_depday(spec)
    # Beide Wege liefern denselben arr-seitigen Zustand (date-16-Landung leckt nicht).
    assert m_same.get('est_arr_iso') == m_none.get('est_arr_iso')
    assert m_same.get('arr_delay_min') == m_none.get('arr_delay_min')


def test_museum_has_expected_inventory():
    """Absicherung, dass die Fixtures überhaupt geladen werden (kein leeres
    Museum durch Pfad-/Glob-Fehler) und die Kern-Stücke präsent sind."""
    ids = {spec.get('id') for _, spec in _iter_ids()}
    assert {'LH867', 'LH890', 'LH423', 'LH1126'} <= ids
    assert len(list(_iter_ids())) >= 5
