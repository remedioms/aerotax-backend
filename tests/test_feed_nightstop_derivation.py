"""FEED-Nightstop-Ableitung `_feed_nightstop_ort` (Florian-Wurzel 2026-07-16).

WARUM: `reader_facts.layover_ort` trug für Florians Tag (MUC→FCO→MUC→FCO, letzte
Ankunft 15:05 in FCO) fälschlich BER — ein Vortags-/Kontext-Wert aus dem Steuer-
Reader. Die Feed-Flächen (friends-today `lay_eff`, Crew-Vergleich/Overlap-Edges,
Family-Roster, Layover-Rec-Discover) lasen dieses rohe Feld blind.

REGEL (hier festgenagelt): Nightstop eines Tages = Ankunfts-IATA des LETZTEN
Flug-Sektors, der am SELBEN (stations-lokalen) Kalendertag ankommt. Ein Red-Eye
(Ankunft am Folgetag) zählt NICHT als Nightstop dieses Tages. Fehlen belastbare
`ical_sectors`, bleibt der bisherige `reader_facts.layover_ort` die Wahrheit
(byte-kompatibel zu Layover-Ruhetagen ohne Legs, z. B. friends-today-Golden Mia).
"""
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(THIS_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app as A


# ── Florians Tag: 3 Sektoren, Turnaround, letzte Ankunft FCO ──────────────────
# MUC→FCO→MUC→FCO am 2026-07-16, letzte Ankunft 15:05 (FCO-lokal ≈ 13:05Z).
# reader_facts.layover_ort=BER ist der FALSCHE Vortags-/Kontext-Wert.
FLORIAN_DAY = {
    'datum': '2026-07-16',
    'klass': 'Z72',
    'routing': 'MUC-FCO-MUC-FCO',
    'reader_facts': {'layover_ort': 'BER',
                     'flight_numbers': ['LH1834', 'LH1835', 'LH1836']},
    'ical_sectors': [
        {'flight': 'LH1834', 'from': 'MUC', 'to': 'FCO',
         'dep_iso': '2026-07-16T06:00:00Z', 'arr_iso': '2026-07-16T07:40:00Z'},
        {'flight': 'LH1835', 'from': 'FCO', 'to': 'MUC',
         'dep_iso': '2026-07-16T08:30:00Z', 'arr_iso': '2026-07-16T10:10:00Z'},
        {'flight': 'LH1836', 'from': 'MUC', 'to': 'FCO',
         'dep_iso': '2026-07-16T11:20:00Z', 'arr_iso': '2026-07-16T13:05:00Z'},
    ],
}


def test_florian_multileg_turnaround_last_arrival_wins():
    """Florian: FCO (letzte Tages-Ankunft), NICHT BER (Vortags-/Kontext-Wert)."""
    assert A._feed_nightstop_ort(FLORIAN_DAY) == 'FCO'


def test_florian_derivation_ignores_wrong_reader_layover():
    """Auch wenn der Reader BER behauptet: die Sektoren-Ableitung überstimmt ihn,
    weil belastbare same-day-Ankünfte vorliegen."""
    assert A._feed_nightstop_ort(FLORIAN_DAY) != 'BER'


def test_redeye_arrival_next_day_is_no_nightstop():
    """Gegentest Red-Eye: Abflug spät, Ankunft am FOLGETAG (stations-lokal) →
    KEIN Nightstop dieses Tages. Ohne Reader-Fallback ⇒ None (kein erfundener
    Ort), und niemals das Red-Eye-Ziel."""
    redeye = {
        'datum': '2026-07-16',
        'reader_facts': {},
        'ical_sectors': [
            {'flight': 'LH500', 'from': 'FRA', 'to': 'GRU',
             'dep_iso': '2026-07-16T22:00:00Z',
             'arr_iso': '2026-07-17T04:30:00Z'},
        ],
    }
    assert A._feed_nightstop_ort(redeye) is None


def test_redeye_does_not_override_reader_with_destination():
    """Red-Eye mit (falschem) Reader-Wert: da es KEINE same-day-Ankunft gibt,
    bleibt der Reader-Fallback stehen — das Red-Eye-Ziel (GRU) wird NICHT als
    Nightstop erfunden."""
    redeye = {
        'datum': '2026-07-16',
        'reader_facts': {'layover_ort': 'BER'},
        'ical_sectors': [
            {'flight': 'LH500', 'from': 'FRA', 'to': 'GRU',
             'dep_iso': '2026-07-16T22:00:00Z',
             'arr_iso': '2026-07-17T04:30:00Z'},
        ],
    }
    got = A._feed_nightstop_ort(redeye)
    assert got != 'GRU'
    assert got == 'BER'


def test_single_leg_normal_same_day_arrival():
    """Gegentest Single-Leg normal: eine Strecke, Ankunft am selben Tag →
    Nightstop = Ziel."""
    single = {
        'datum': '2026-07-16',
        'reader_facts': {'layover_ort': 'LHR'},
        'ical_sectors': [
            {'flight': 'LH900', 'from': 'FRA', 'to': 'LHR',
             'dep_iso': '2026-07-16T09:00:00Z',
             'arr_iso': '2026-07-16T09:40:00Z'},
        ],
    }
    assert A._feed_nightstop_ort(single) == 'LHR'


def test_legless_layover_day_keeps_reader_fallback():
    """Reiner Layover-Ruhetag ohne Legs (friends-today-Golden Mia): kein
    ical_sectors → bisheriges reader_facts.layover_ort bleibt (byte-kompatibel)."""
    mia = {'datum': '2026-07-16',
           'reader_facts': {'layover_ort': 'SFO'}, 'ical_sectors': []}
    assert A._feed_nightstop_ort(mia) == 'SFO'


def test_no_sectors_no_reader_is_none():
    """Weder Sektoren noch Reader-Wert → None (kein erfundener Ort)."""
    assert A._feed_nightstop_ort({'datum': '2026-07-16',
                                  'reader_facts': {}}) is None


def test_missing_datum_falls_back_to_reader():
    """Ohne parsbares Datum keine Sektoren-Ableitung → Reader-Fallback, wirft nie."""
    assert A._feed_nightstop_ort({'reader_facts': {'layover_ort': 'JFK'}}) == 'JFK'


def test_non_dict_input_is_safe():
    """Robustheit: Nicht-Dict-Eingabe wirft nie."""
    assert A._feed_nightstop_ort(None) is None
    assert A._feed_nightstop_ort([]) is None


def test_florian_remote_last_arrival_survives_homebase_gate():
    """Florians letzte Ankunft (FCO) ist NICHT seine Homebase (MUC) → auch mit
    Homebase-Gate bleibt FCO der Nightstop (echte Übernachtung außerhalb)."""
    assert A._feed_nightstop_ort(FLORIAN_DAY, homebase='MUC') == 'FCO'


def test_same_day_turnaround_home_is_no_layover():
    """Same-Day-Turnaround ZURÜCK zur Homebase (BCN→FRA→ARN→FRA, HB=FRA): mit
    Homebase-Gate KEIN Basis-Pin → Reader-Fallback (hier None). Das ist die
    friends-today-Live-Semantik (Tibor-Livefall): die Live-Kaskade zeigt
    „unterwegs", nicht „Layover Frankfurt"."""
    day = {
        'datum': '2026-07-16',
        'reader_facts': {'layover_ort': None},
        'ical_sectors': [
            {'flight': 'LH1139', 'from': 'BCN', 'to': 'FRA',
             'dep_iso': '2026-07-16T05:40:00Z', 'arr_iso': '2026-07-16T07:55:00Z'},
            {'flight': 'LH802', 'from': 'FRA', 'to': 'ARN',
             'dep_iso': '2026-07-16T09:25:00Z', 'arr_iso': '2026-07-16T11:15:00Z'},
            {'flight': 'LH803', 'from': 'ARN', 'to': 'FRA',
             'dep_iso': '2026-07-16T12:30:00Z', 'arr_iso': '2026-07-16T14:20:00Z'},
        ],
    }
    assert A._feed_nightstop_ort(day, homebase='FRA') is None
    # OHNE Homebase-Gate (statische Ziel-Flächen) liefert es den Ziel-Ort FRA.
    assert A._feed_nightstop_ort(day) == 'FRA'


def test_outbound_layover_last_arrival_is_remote_station():
    """Klassischer Auslands-Layover (FRA→JFK am selben Tag): Nightstop = JFK,
    auch wenn der Reader-Wert übereinstimmt (kein Regressionsrisiko für den
    friends-today-Golden Kai)."""
    kai = {
        'datum': '2026-07-09',
        'reader_facts': {'layover_ort': 'JFK'},
        'ical_sectors': [
            {'flight': 'LH400', 'from': 'FRA', 'to': 'JFK',
             'dep_iso': '2026-07-09T08:00:00Z',
             'arr_iso': '2026-07-09T16:30:00Z'},
        ],
    }
    assert A._feed_nightstop_ort(kai) == 'JFK'
