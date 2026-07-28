"""PHANTOM-KALENDER-PUSHES — Owner-Fall 30.07.2026 + Flip-Flop-Hysterese.

Owner Miguel (AT-AA10DE7B8DBD45E7), 2026-07-27 22:14:41 UTC:
„ich habe einen kalender push change für den 30ten bekommen obwohl nichts neues.
das ist nervig und stressig für crews."

Der Eintrag aus der Prod-Tabelle `roster_changes` (unten 1:1 als Fixture) zeigt
Feld für Feld, dass sich am DIENST nichts geändert hat:

    ical_sectors   LH455 SFO→FRA  dep 2026-07-30T21:40:00Z  arr …T08:25:00Z
                   IDENTISCH auf beiden Seiten
    routing        'SFO-FRA'      IDENTISCH
    layover_ort    'SFO'          IDENTISCH
    end_time       '10:25'        IDENTISCH
    marker         + „ · 12:40 LT Pickup SFO"      ← einziger echter Unterschied
    start_time     '23:40' → '21:40'               ← Folge davon
    ical_start_iso 21:40Z → 19:40Z                 ← Folge davon

Wurzel: `ical_start` saugt die Pickup-Zeit auf. Erscheint der Pickup (hier durch
den am selben Tag deployten Pickup-iCal-Fallback `merge_ical_pickups`), springt
`start_time` um den PU-Vorlauf — die alte Whitelist wertete BEIDES als Änderung
(„PU neu" UND „Meldezeit verschoben") und pushte.

Fleet-Messung derselben Nacht (188 User, 270 Einträge in 24 h):
  · 212 von 219 'modified' hätten gepusht,
  · 107 davon ausgelöst NUR von `klass` (101 bei identischen Legs/Zeiten),
  · 51 von 52 mehrfach belegten Tag-Zellen oszillierten A→B→A (13 User),
    teils mit zwei gegenläufigen Einträgen in DERSELBEN Sekunde — drei
    iOS-Aufrufer von `takeRosterSnapshot` schreiben konkurrierende Snapshots.

Dieser Test hält beides fest: die Substanz-Regel (kein Diff, kein Push) und die
Hysterese (ein pendelnder Tag pusht höchstens EINMAL in 24 h).
"""
import json
import os
import sys
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as A


# ══════════════════════════════════════════════════════════════════════════════
# 1 · Der echte Prod-Eintrag (roster_changes, token AT-AA10DE7B8DBD45E7)
# ══════════════════════════════════════════════════════════════════════════════
def _miguel_3007(datum='2026-07-30', mit_pickup=True):
    """Miguels 30.07. — beide Seiten des Prod-Changes, 1:1 aus Supabase."""
    marker = ('Layover [SFO] (Tag 3/3) · 12:40 LT Pickup SFO · '
              'LH 455: SFO-FRA (Tag 1/2)' if mit_pickup else
              'Layover [SFO] (Tag 3/3) · LH 455: SFO-FRA (Tag 1/2)')
    return {
        'datum': datum,
        'marker': marker,
        'routing': 'SFO-FRA',
        'ical_sectors': [{'to': 'FRA', 'from': 'SFO', 'flight': 'LH455',
                          'dep_iso': f'{datum}T21:40:00Z',
                          'arr_iso': '2026-07-31T08:25:00Z'}],
        'reader_facts': {
            'end_time': '10:25',
            'marker_raw': marker,
            'start_time': '21:40' if mit_pickup else '23:40',
            'layover_ort': 'SFO',
            'overnight_after_day': True,
        },
        'ical_start_iso': (f'{datum}T19:40:00+00:00' if mit_pickup
                           else f'{datum}T21:40:00+00:00'),
    }


def test_owner_3007_pickup_auftauchen_ist_keine_aenderung():
    ohne, mit = _miguel_3007(mit_pickup=False), _miguel_3007(mit_pickup=True)
    # Feld-Beweis: der Dienst selbst ist auf beiden Seiten identisch.
    assert ohne['ical_sectors'] == mit['ical_sectors']
    assert ohne['routing'] == mit['routing']
    assert (ohne['reader_facts']['layover_ort']
            == mit['reader_facts']['layover_ort'])
    # … nur der Pickup taucht auf und zieht start_time mit.
    assert A._rc_pickup_hhmm(ohne) == '' and A._rc_pickup_hhmm(mit) == '12:40'
    assert ohne['reader_facts']['start_time'] != mit['reader_facts']['start_time']
    # → weder Verlauf-Eintrag noch Push, in beide Richtungen.
    assert A._rc_meaningfully_modified(ohne, mit) is False
    assert A._rc_meaningfully_modified(mit, ohne) is False
    assert A._compute_roster_diff([ohne], [mit], today='2026-07-27') == []
    assert A._compute_roster_diff([mit], [ohne], today='2026-07-27') == []


def test_owner_3007_echte_aenderungen_pushen_weiterhin():
    basis = _miguel_3007(mit_pickup=True)
    # Abflug 40 min später → echte Änderung.
    spaeter = json.loads(json.dumps(basis))
    spaeter['ical_sectors'][0]['dep_iso'] = '2026-07-30T22:20:00Z'
    assert A._rc_meaningfully_modified(basis, spaeter) is True
    # Anderer Flug → echte Änderung.
    anders = json.loads(json.dumps(basis))
    anders['ical_sectors'][0]['flight'] = 'LH457'
    assert A._rc_meaningfully_modified(basis, anders) is True
    # Anderer Layover-Ort → echte Änderung.
    lay = json.loads(json.dumps(basis))
    lay['reader_facts']['layover_ort'] = 'LAX'
    assert A._rc_meaningfully_modified(basis, lay) is True
    # Pickup 45 min vorverlegt → echte Änderung.
    pu = json.loads(json.dumps(basis))
    pu['marker'] = pu['reader_facts']['marker_raw'] = (
        'Layover [SFO] (Tag 3/3) · 11:55 LT Pickup SFO · LH 455: SFO-FRA (Tag 1/2)')
    assert A._rc_meaningfully_modified(basis, pu) is True
    # Reine Blockzeiten-Pflege (Ankunft +17 min, Dienstende nach) → still.
    block = json.loads(json.dumps(basis))
    block['ical_sectors'][0]['arr_iso'] = '2026-07-31T08:42:00Z'
    block['reader_facts']['end_time'] = '10:42'
    assert A._rc_meaningfully_modified(basis, block) is False


# ══════════════════════════════════════════════════════════════════════════════
# 1b · Weitere live gemessene Rausch-Quellen derselben Bauart:
#      ein MARKER-ELEMENT kommt/geht → `ical_start` springt → `start_time`
#      springt. Alle drei Fälle stammen aus der Fleet-Messung 27./28.07.
# ══════════════════════════════════════════════════════════════════════════════
def _swiss_nrt(mit_briefing=True):
    """Live-Fall: start_time pendelte 04:29 ↔ 11:35 (7 h), weil der Marker das
    Element „18:35 LT Briefing NRT" verlor und wiederbekam — LX161 NRT-ZRH war
    beide Male identisch."""
    m = ('18:35 LT Briefing NRT · LX161 NRT 1129 ZRH 1850 77W [FA] · '
         'LAYOVER (Tag 3/3)' if mit_briefing else
         'LX161 NRT 1129 ZRH 1850 77W [FA] · LAYOVER (Tag 3/3)')
    return {'datum': '2026-07-13', 'marker': m, 'routing': 'NRT-ZRH',
            'ical_sectors': [{'flight': 'LX161', 'from': 'NRT', 'to': 'ZRH',
                              'dep_iso': '2026-07-13T02:29:00Z',
                              'arr_iso': '2026-07-13T16:50:00Z'}],
            'reader_facts': {'marker_raw': m, 'layover_ort': 'ZRH',
                             'start_time': '11:35' if mit_briefing else '04:29',
                             'end_time': '19:50'}}


def test_briefing_marker_praesenz_flip_ist_still():
    ohne, mit = _swiss_nrt(False), _swiss_nrt(True)
    assert A._rc_briefing_hhmm(mit) == '18:35' and A._rc_briefing_hhmm(ohne) == ''
    assert A._rc_meaningfully_modified(ohne, mit) is False
    assert A._rc_meaningfully_modified(mit, ohne) is False


def test_echte_briefing_verschiebung_pusht():
    mit = _swiss_nrt(True)
    spaeter = json.loads(json.dumps(mit))
    spaeter['marker'] = spaeter['reader_facts']['marker_raw'] = (
        '19:20 LT Briefing NRT · LX161 NRT 1129 ZRH 1850 77W [FA] · LAYOVER (Tag 3/3)')
    assert A._rc_briefing_hhmm(spaeter) == '19:20'
    assert A._rc_meaningfully_modified(mit, spaeter) is True
    # 3 Minuten darunter nicht.
    knapp = json.loads(json.dumps(mit))
    knapp['marker'] = knapp['reader_facts']['marker_raw'] = (
        '18:38 LT Briefing NRT · LX161 NRT 1129 ZRH 1850 77W [FA] · LAYOVER (Tag 3/3)')
    assert A._rc_meaningfully_modified(mit, knapp) is False


def _muc_icn(mit_x=True, start='02:55'):
    """Live-Fall: das Marker-Element „X" kam/ging und schob start_time um exakt
    30 min — LH718 MUC-ICN unverändert (23 solcher Fälle in 24 h)."""
    m = ('LH 718: MUC-ICN (Tag 2/2) · X · Layover [ICN] (Tag 1/3)' if mit_x else
         'LH 718: MUC-ICN (Tag 2/2) · Layover [ICN] (Tag 1/3)')
    return {'datum': '2026-08-16', 'marker': m, 'routing': 'MUC-ICN',
            'ical_sectors': [{'flight': 'LH718', 'from': 'MUC', 'to': 'ICN',
                              'dep_iso': '2026-08-16T13:00:00Z',
                              'arr_iso': '2026-08-17T04:30:00Z'}],
            'reader_facts': {'marker_raw': m, 'layover_ort': 'ICN',
                             'start_time': start, 'end_time': '06:30'}}


def test_marker_element_flip_ist_still():
    assert A._rc_meaningfully_modified(_muc_icn(True, '02:55'),
                                       _muc_icn(False, '03:25')) is False
    assert A._rc_meaningfully_modified(_muc_icn(False, '03:25'),
                                       _muc_icn(True, '02:55')) is False


def test_fortsetzungs_suffix_tag_n_m_ist_still():
    # „Layover [KRK]" ↔ „Layover [KRK] (Tag 2/2)" schob start_time um 13 h.
    a = _muc_icn(True, '02:55')
    b = json.loads(json.dumps(a))
    b['marker'] = b['reader_facts']['marker_raw'] = (
        'LH 718: MUC-ICN (Tag 1/2) · X · Layover [ICN]')
    b['reader_facts']['start_time'] = '13:25'
    assert A._rc_meaningfully_modified(a, b) is False


def test_report_shift_bei_identischem_marker_pusht():
    # Marker byte-identisch, Meldezeit 30 min früher → echte Änderung.
    a = _muc_icn(True, '02:55')
    b = _muc_icn(True, '02:25')
    assert A._rc_meaningfully_modified(a, b) is True


# ══════════════════════════════════════════════════════════════════════════════
# 1c · Degradations-Guard: Massen-Wegfall ist ein kaputter Import
# ══════════════════════════════════════════════════════════════════════════════
def test_massenwegfall_erzeugt_keine_dienst_entfernt_flut():
    # Live: 32 'removed' bei EINEM User in EINER Sekunde (+5 bei einem zweiten,
    # 3 betroffene User insgesamt) — das ist ein abgeschnittener Import.
    old = [_tagx(f'2026-08-{d:02d}') for d in range(1, 21)]
    assert A._compute_roster_diff(old, [], today='2026-07-28') == []
    # EIN gestrichener Tag bleibt selbstverständlich eine echte Änderung.
    d = A._compute_roster_diff(old, old[:-1], today='2026-07-28')
    assert len(d) == 1 and d[0]['kind'] == 'removed'


def _tagx(datum):
    return {'datum': datum, 'klass': 'Flug', 'routing': 'FRA-JFK',
            'reader_facts': {'start_time': '08:00', 'end_time': '18:00'}}


# ══════════════════════════════════════════════════════════════════════════════
# 2 · Flip-Flop-Hysterese (_rc_hysteresis_filter)
# ══════════════════════════════════════════════════════════════════════════════
def _ch(datum, new, kind='modified', old=None):
    return {'datum': datum, 'kind': kind, 'old': old or {}, 'new': new}


def _tag(datum, dep_hh):
    return {'datum': datum, 'klass': 'Flug', 'routing': 'FRA-IAH',
            'ical_sectors': [{'flight': 'LH440', 'from': 'FRA', 'to': 'IAH',
                              'dep_iso': f'{datum}T{dep_hh}:00:00Z',
                              'arr_iso': f'{datum}T18:30:00Z'}],
            'reader_facts': {'start_time': '07:10', 'end_time': '18:30'}}


def test_hysterese_pendeln_kostet_genau_einen_push():
    state = {}
    a, b = _tag('2026-08-02', '08'), _tag('2026-08-02', '10')
    # A → B: neuer Zustand, pusht.
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-02', b)])) == 1
    # B → A: zurueckgekippt auf einen Zustand, der schon bekannt ist? A wurde
    # noch nie als ZIEL gepusht → einmal erlaubt.
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-02', a)])) == 1
    # Ab hier pendelt es nur noch: JEDER weitere Wechsel ist still.
    for _ in range(6):
        assert A._rc_hysteresis_filter(state, [_ch('2026-08-02', b)]) == []
        assert A._rc_hysteresis_filter(state, [_ch('2026-08-02', a)]) == []


def test_hysterese_neuer_zustand_pusht_sofort_wieder():
    state = {}
    a, b, c = (_tag('2026-08-02', '08'), _tag('2026-08-02', '10'),
               _tag('2026-08-02', '12'))
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-02', a)])) == 1
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-02', b)])) == 1
    assert A._rc_hysteresis_filter(state, [_ch('2026-08-02', a)]) == []
    # Ein DRITTER, nie gesehener Zustand ist eine echte Folge-Änderung.
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-02', c)])) == 1


def test_hysterese_ist_pro_tag_und_laeuft_nach_24h_ab():
    state = {}
    a2, a3 = _tag('2026-08-02', '08'), _tag('2026-08-03', '08')
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-02', a2)])) == 1
    # Anderer Tag, gleiche Struktur → eigener Zustandsraum, pusht.
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-03', a3)])) == 1
    assert A._rc_hysteresis_filter(state, [_ch('2026-08-02', a2)]) == []
    # 25 h später ist die Sperre abgelaufen.
    spaeter = datetime.now() + timedelta(hours=25)
    assert len(A._rc_hysteresis_filter(state, [_ch('2026-08-02', a2)],
                                       now=spaeter)) == 1


def test_hysterese_defensiv():
    assert A._rc_hysteresis_filter(None, [{'datum': 'x', 'kind': 'modified'}])
    assert A._rc_hysteresis_filter({}, []) == []
    assert A._rc_hysteresis_filter({}, None) == []
    # removed/added desselben Tags oszillieren ueber die Leer-Signatur.
    state = {}
    t = _tag('2026-08-02', '08')
    assert len(A._rc_hysteresis_filter(
        state, [_ch('2026-08-02', None, kind='removed', old=t)])) == 1
    assert A._rc_hysteresis_filter(
        state, [_ch('2026-08-02', None, kind='removed', old=t)]) == []


# ══════════════════════════════════════════════════════════════════════════════
# 3 · Endpoint: derselbe Tag pendelt → genau EIN Push
# ══════════════════════════════════════════════════════════════════════════════
def _snapshot_env(tmp_path, old_tage_box):
    changes_file = tmp_path / 'roster_changes_test.json'
    push = MagicMock()
    return (
        patch.object(A, '_roster_snapshot_read',
                     side_effect=lambda _t: {'tage': old_tage_box[0]}),
        patch.object(A, '_roster_snapshot_save', return_value=True),
        patch.object(A, '_roster_snapshot_path',
                     return_value=str(tmp_path / 'snap.json')),
        patch.object(A, '_roster_changes_path', return_value=str(changes_file)),
        patch.object(A, '_crew_flight_ingest', return_value=None),
        patch.object(A, '_push_notify_async', push),
        patch.object(A, '_profile_homebase_cached', return_value='FRA'),
        push,
        changes_file,
    )


def test_endpoint_flipflop_pusht_hoechstens_einmal(tmp_path):
    """Zwei konkurrierende Snapshot-Schreiber (die Live-Signatur) posten
    abwechselnd ihren Stand — der User darf höchstens EINEN Push sehen."""
    d = (date.today() + timedelta(days=3)).isoformat()
    mit = dict(_miguel_3007(datum=d, mit_pickup=True))
    ohne = dict(_miguel_3007(datum=d, mit_pickup=False))
    # Abflug unterscheidet die beiden Zustaende ECHT (≥ 5 min) — sonst greift
    # schon das Substanz-Gate und die Hysterese käme nie zum Zug.
    mit['ical_sectors'] = [dict(mit['ical_sectors'][0], dep_iso=f'{d}T22:30:00Z')]
    box = [[ohne]]
    p = _snapshot_env(tmp_path, box)
    push, changes_file = p[7], p[8]
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        client = A.app.test_client()
        for i in range(8):
            nxt = mit if i % 2 == 0 else ohne
            r = client.post('/api/user/roster-snapshot/testtoken123',
                            json={'tage': [nxt]})
            assert r.status_code == 200
            box[0] = [nxt]
    # 8 Wechsel, aber hoechstens 2 Pushes (der erste je Zielzustand).
    assert push.call_count <= 2, push.call_count
    # Der Verlauf bleibt vollstaendig — die Liste in der App zeigt weiterhin
    # jede substanzielle Aenderung.
    assert len(json.loads(changes_file.read_text())['pending']) == 8


def test_endpoint_owner_3007_kein_push_und_kein_verlauf(tmp_path):
    """Miguels echter Fall am Endpoint: der Pickup taucht auf → nichts."""
    d = (date.today() + timedelta(days=3)).isoformat()
    ohne = _miguel_3007(datum=d, mit_pickup=False)
    mit = _miguel_3007(datum=d, mit_pickup=True)
    box = [[ohne]]
    p = _snapshot_env(tmp_path, box)
    push, changes_file = p[7], p[8]
    with p[0], p[1], p[2], p[3], p[4], p[5], p[6]:
        r = A.app.test_client().post('/api/user/roster-snapshot/testtoken123',
                                     json={'tage': [mit]})
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0
