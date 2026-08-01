"""Geplante Hangouts — `meta.visible_from` (Owner 2026-08-01).

Anlass: eine kuratierte Event-Reihe (Stadtfeste + Open-Air-Kino eines Sommers)
soll vorab angelegt, aber gestaffelt eingeblendet werden. Bisher gab es kein
„frühestens sichtbar ab": `_hangouts_load_all_active` nimmt alles mit
`pin_date >= heute`, also stünde der ganze August ab dem 1. im Feed.

Die Regel, die diese Datei festnagelt:

  * `visible_from` in der Zukunft → fremde Viewer sehen den Hangout NICHT,
    auf keinem der drei Wege (Feed, Karte, Detail/Chat-Kontext).
  * FAIL-OPEN — fehlt der Wert oder ist er Müll, ist der Hangout SICHTBAR.
    Das ist die Umkehrung des `audience`-Filters und volle Absicht: eine
    Terminplanung darf einen echten User-Treff nie verschlucken.
  * Der ERSTELLER sieht seinen Hangout immer, auch davor.
  * Kein Geo-Push beim Anlegen, solange der Hangout noch nicht sichtbar ist.

SICHERHEIT: kein echter SB-/APNs-Call — alle Loader sind gemockt.
"""
import json
import os

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import sys
from unittest.mock import patch

import pytest

import app as A


@pytest.fixture(autouse=True)
def _pin_app():
    prev = sys.modules.get('app')
    sys.modules['app'] = A
    yield
    if prev is not None:
        sys.modules['app'] = prev


VIEWER = 'viewer-token'
OWNER = 'owner-token'
LH_FRA_CABIN = {'airline': 'Lufthansa', 'homebase': 'FRA', 'position': 'PU'}


def _iso(days):
    return (A.datetime.now(A.timezone.utc)
            + A.timedelta(days=days)).isoformat()


def _pin(pid='h1', owner=OWNER, meta=None, iata='FRA'):
    row = {'id': pid, 'user_token': owner, 'iata_code': iata,
           'lat': 50.0, 'lng': 8.5, 'pin_date': None,
           'note': '🍎 Apfelweinfestival',
           'created_at': A.datetime.now(A.timezone.utc).isoformat()}
    if meta is not None:
        row['meta'] = meta
    return row


def _list_hangouts(pins, token=VIEWER):
    with patch.object(A, '_hangouts_load_all_active', return_value=pins), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load',
                      return_value={'profile': LH_FRA_CABIN}), \
         A.app.test_request_context(f'/api/user/hangouts/{token}'):
        return A.list_hangouts(token).get_json()


# ── Der Parser: tolerant, wirft nie ────────────────────────────────────────

def test_visible_from_fehlt_ist_none():
    assert A._hangout_visible_from(_pin()) is None
    assert A._hangout_visible_from(_pin(meta={'v': 1})) is None
    assert A._hangout_visible_from(_pin(meta={'v': 1, 'visible_from': ''})) is None


def test_visible_from_muell_ist_none_statt_crash():
    """Unlesbar zählt wie „nicht gesetzt" — fail-open, kein Werfen."""
    for junk in ('morgen', '2026-13-45', 42, None, [], {'a': 1}, '   '):
        assert A._hangout_visible_from(_pin(meta={'v': 1,
                                                  'visible_from': junk})) is None
        assert A._hangout_is_scheduled_ahead(
            _pin(meta={'v': 1, 'visible_from': junk})) is False


def test_visible_from_ohne_offset_wird_als_utc_gelesen():
    """Kein Raten einer Zone — naive Werte sind UTC (Zeitzonen-Fehlerklasse)."""
    dt = A._hangout_visible_from(
        _pin(meta={'v': 1, 'visible_from': '2026-08-22T10:00:00'}))
    assert dt is not None and dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0


def test_visible_from_akzeptiert_z_und_offset():
    a = A._hangout_visible_from(
        _pin(meta={'v': 1, 'visible_from': '2026-08-22T10:00:00Z'}))
    b = A._hangout_visible_from(
        _pin(meta={'v': 1, 'visible_from': '2026-08-22T12:00:00+02:00'}))
    assert a is not None and b is not None and a == b


def test_meta_normalize_laesst_visible_from_durch():
    meta = A._hangout_meta_normalize({'visible_from': '2026-08-22T10:00:00Z',
                                      'quatsch': 'x'})
    assert meta['visible_from'] == '2026-08-22T10:00:00Z'
    assert 'quatsch' not in meta


# ── Feed ───────────────────────────────────────────────────────────────────

def test_feed_versteckt_den_noch_nicht_eingeblendeten():
    row = _pin(meta={'v': 1, 'visible_from': _iso(21)})
    assert _list_hangouts([row])['hangouts'] == []


def test_feed_zeigt_ihn_sobald_der_zeitpunkt_durch_ist():
    row = _pin(meta={'v': 1, 'visible_from': _iso(-1)})
    assert [h['id'] for h in _list_hangouts([row])['hangouts']] == ['h1']


def test_feed_ohne_visible_from_bleibt_sichtbar():
    """Der Normalfall: ein echter User-Treff hat den Wert gar nicht."""
    assert [h['id'] for h in _list_hangouts([_pin()])['hangouts']] == ['h1']


def test_feed_muell_versteckt_NICHT():
    """FAIL-OPEN. Ein Parse-Fehler darf keinen echten Treff verschlucken."""
    row = _pin(meta={'v': 1, 'visible_from': 'irgendwann im August'})
    assert [h['id'] for h in _list_hangouts([row])['hangouts']] == ['h1']


def test_feed_ersteller_sieht_seinen_geplanten_immer():
    row = _pin(meta={'v': 1, 'visible_from': _iso(21)})
    h = _list_hangouts([row], token=OWNER)['hangouts']
    assert [x['id'] for x in h] == ['h1'] and h[0]['mine'] is True


def test_feed_staffelt_eine_ganze_reihe():
    """Der eigentliche Zweck: 3 Termine angelegt, nur der fällige steht drin."""
    rows = [_pin('jetzt', meta={'v': 1, 'visible_from': _iso(-2)}),
            _pin('bald', meta={'v': 1, 'visible_from': _iso(7)}),
            _pin('spaet', meta={'v': 1, 'visible_from': _iso(28)})]
    assert [h['id'] for h in _list_hangouts(rows)['hangouts']] == ['jetzt']


# ── Karte (zweiter Auslieferungsweg derselben Hangouts) ────────────────────

def _karte(row):
    with patch.object(A, '_manual_pins_load', return_value=[]), \
         patch.object(A, '_manual_pins_for_friends', return_value=[]), \
         patch.object(A, '_public_pins_at_iatas', return_value=[row]), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load',
                      return_value={'profile': LH_FRA_CABIN}), \
         patch.object(A, '_user_current_iata', return_value='FRA'), \
         patch.object(A, '_user_future_layovers', return_value=[]), \
         A.app.test_request_context(f'/api/user/crew-at-destination/{VIEWER}'):
        return A.get_crew_at_destination(VIEWER).get_json()


def test_karte_ist_keine_hintertuer():
    """crew-at-destination liefert dieselben Pins — gleicher Filter, sonst
    stünde der geplante Hangout auf der Karte, bevor er im Feed steht.

    Mit Gegenprobe: derselbe Pin, nur mit fälligem `visible_from`, MUSS
    ankommen. Sonst wäre der Test auch ohne den Filter grün und würde nur
    beweisen, dass das Mock-Gerüst nichts durchlässt.
    """
    assert _karte(_pin(meta={'v': 1,
                             'visible_from': _iso(21)}))['manual_pins'] == []
    durch = _karte(_pin(meta={'v': 1, 'visible_from': _iso(-1)}))['manual_pins']
    assert [p['id'] for p in durch] == ['h1']


# ── Detail / Chat-Kontext ──────────────────────────────────────────────────

def test_detail_ist_404_nicht_403_solange_geplant():
    """Nicht sichtbar = nicht von „gibt es nicht" zu unterscheiden."""
    row = _pin(meta={'v': 1, 'visible_from': _iso(21)})
    with patch.object(A, '_hangout_load_one', return_value=row), \
         patch.object(A, '_profile_load',
                      return_value={'profile': LH_FRA_CABIN}):
        assert A._hangout_visible_row(VIEWER, 'h1') is None
        assert A._hangout_visible_row(OWNER, 'h1') is row  # Ersteller: immer


# ── Kuratierte Termine (`meta.curated`) ────────────────────────────────────

def test_curated_zaehlt_den_ersteller_NICHT_als_zusager():
    """AeroX legt das Museumsuferfest an, geht aber nicht hin. „1 dabei" mit
    AeroX-Avatar wäre ein erfundener Wert."""
    row = _pin(meta={'v': 1, 'curated': '1'})
    assert A._hangout_attendees_of_row(row) == []
    h = _list_hangouts([row])['hangouts'][0]
    assert h['attendee_count'] == 0 and h['attending'] is False


def test_normaler_hangout_zaehlt_den_ersteller_weiter_mit():
    """Regression: bei einem ECHTEN Treff bleibt „wer einlädt, ist da"."""
    assert A._hangout_attendees_of_row(_pin()) == [OWNER]
    assert _list_hangouts([_pin()])['hangouts'][0]['attendee_count'] == 1


def test_curated_zaehlt_ECHTE_zusagen_ganz_normal():
    """Weg fällt NUR das automatische Mitzählen des Erstellers.

    Ein Token, das wirklich in `attendees` steht, ist eine gespeicherte
    Zusage — die würde hier still zu verschlucken Datenverlust bedeuten. Der
    Erstell-Endpoint schreibt für kuratierte Termine `attendees = []`, der
    Fall unten entsteht also nur durch ein echtes „Bin dabei".
    """
    row = _pin(meta={'v': 1, 'curated': '1'})
    row['attendees'] = [VIEWER]
    assert A._hangout_attendees_of_row(row) == [VIEWER]
    h = _list_hangouts([row])['hangouts'][0]
    assert h['attendee_count'] == 1 and h['attending'] is True


def test_curated_nur_bei_exakt_eins():
    """Kein Wahrheitswert-Geraten: nur '1' schaltet um."""
    for v in ('0', '', 'true', 1, True, None):
        assert A._hangout_is_curated(_pin(meta={'v': 1, 'curated': v})) is False
    assert A._hangout_is_curated(_pin(meta={'v': 1, 'curated': '1'})) is True


def test_curated_ueberlebt_meta_als_json_string():
    """`meta` kommt je nach Treiber als String zurück — tolerant bleiben."""
    row = _pin()
    row['meta'] = json.dumps({'v': 1, 'curated': '1'})
    assert A._hangout_is_curated(row) is True
    assert A._hangout_attendees_of_row(row) == []


def test_meta_normalize_laesst_curated_und_all_day_durch():
    meta = A._hangout_meta_normalize({'curated': '1', 'all_day': '1'})
    assert meta['curated'] == '1' and meta['all_day'] == '1'


# ── Push ───────────────────────────────────────────────────────────────────

def test_kein_geo_push_fuer_einen_geplanten_hangout():
    """Ein Push Wochen vor dem Einblenden liefe in einen 404 — und 31 Seed-
    Hangouts wären 31 Pushes an halb Rhein-Main in einer Nacht."""
    assert A._hangout_is_scheduled_ahead(
        {'meta': {'v': 1, 'visible_from': _iso(21)}}) is True
    assert A._hangout_is_scheduled_ahead(
        {'meta': {'v': 1, 'visible_from': _iso(-1)}}) is False
    # Degradiert (Migration fehlt, `meta` geschluckt) → sofort sichtbar,
    # also ist der Push dann richtig.
    assert A._hangout_is_scheduled_ahead({'meta': None}) is False


# ── Homebase zählt als „hier" (Owner 2026-08-01) ───────────────────────────

def _crew_dest(pins_at_iatas, profile, cur_iata=None):
    """crew-at-destination mit LEEREM Roster — genau der Fall, um den es geht:
    freier Tag, keine Layover, kein heutiger Roster-Eintrag."""
    seen = {}

    def _capture(iatas):
        seen['iatas'] = set(iatas or [])
        return [p for p in pins_at_iatas
                if (p.get('iata_code') or '').upper() in seen['iatas']]

    with patch.object(A, '_manual_pins_load', return_value=[]), \
         patch.object(A, '_manual_pins_for_friends', return_value=[]), \
         patch.object(A, '_public_pins_at_iatas', side_effect=_capture), \
         patch.object(A, '_friends_load', return_value={'friends': []}), \
         patch.object(A, '_profiles_load_bulk', return_value={}), \
         patch.object(A, '_profile_load', return_value={'profile': profile}), \
         patch.object(A, '_user_current_iata', return_value=cur_iata), \
         patch.object(A, '_user_future_layovers', return_value=[]), \
         A.app.test_request_context(f'/api/user/crew-at-destination/{VIEWER}'):
        payload = A.get_crew_at_destination(VIEWER).get_json()
    return payload, seen.get('iatas', set())


def test_homebase_zaehlt_auch_ohne_dienstplan_eintrag():
    """FRA-Baser zu Hause, freier Tag, kein Roster → sieht seine lokalen
    Hangouts trotzdem. Vorher war die IATA-Menge in genau diesem Fall LEER."""
    pin = _pin(owner=OWNER, iata='FRA')
    payload, iatas = _crew_dest([pin], {'homebase': 'FRA'})
    assert 'FRA' in iatas
    assert [p['id'] for p in payload['manual_pins']] == ['h1']


def test_ohne_homebase_im_profil_keine_erfundene_iata():
    """Kein Profil-Feld → nichts dazuerfinden."""
    for prof in ({}, {'homebase': ''}, {'homebase': 'Frankfurt'}, {'homebase': 'F1A'}):
        _, iatas = _crew_dest([], prof)
        assert iatas == set(), f'{prof} → {iatas}'


def test_homebase_erweitert_nicht_auf_alles():
    """Nur die Base kommt dazu — ein Hangout anderswo bleibt unsichtbar."""
    payload, iatas = _crew_dest([_pin(owner=OWNER, iata='JFK')], {'homebase': 'FRA'})
    assert iatas == {'FRA'} and payload['manual_pins'] == []


def test_homebase_neben_aktuellem_standort():
    """Beide zählen: Base UND wo ich heute laut Roster bin."""
    _, iatas = _crew_dest([], {'homebase': 'FRA'}, cur_iata='BOS')
    assert iatas == {'FRA', 'BOS'}
