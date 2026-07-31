"""Dienstplan-Push: konkreter Inhalt + Vergangenheits-/Pickup-Gates (Flo Z,
FO, 2026-07-20):

  (a) „WAS sich geändert hat, sieht man nicht" → der Push-Body nennt jetzt die
      erste konkrete Änderung („Mi 22.07: LH440 FRA-IAH neu" / „Di 21.07:
      Briefing 09:40 → 10:15") + „(+N weitere)". Formatter:
      _roster_changes_push_body / _roster_change_push_line. Max ~120 Zeichen.
  (b) „Push kommt auch, wenn die Tour vorbei ist" → Push-Gates in
      take_roster_snapshot:
        • _roster_change_is_past: Tage VOR heute (Homebase-lokal) pushen nicht.
        • _roster_change_is_push_worthy → _rc_meaningfully_modified: das EINE
          Substanz-Gate (2026-07-28). Die früheren Einzelfunktionen
          _roster_change_is_pickup_prune und _roster_change_is_blocktime_drift
          sind darin aufgegangen und ersatzlos entfallen.

OWNER-ENTSCHEID 2026-07-29 (Screenshot Build 246): Gate 4
(_rc_duty_substance_changed) galt zunächst NUR für den Push, der Verlauf blieb
bewusst vollständig. Genau das hat der Owner gekippt — die Liste
„Dienstplan-Änderungen" stand voll mit reinen ZEIT-Einträgen („Abflug LH454:
10:25 → 10:55 · Ankunft 12:40 → 13:10", direkt darunter dieselbe Änderung
rückwärts: das LH454-Ping-Pong). Owner wörtlich: „Das sollte nicht mal
aufpoppen. Das ist nicht wichtig!!!! Einfach nur big changes."
Seitdem filtert Gate 4 schon in _compute_roster_diff: ein 'modified' ohne
DIENST-Substanz erzeugt GAR KEINEN Eintrag (kein pending, kein Verlauf, kein
Badge). Die Zeit-Änderung selbst wird trotzdem übernommen — der Snapshot wird
unabhängig vom Diff geschrieben. added/removed sind unberührt.

KEIN echtes APNs/SB: _push_notify_async & Co. werden gemockt (Muster
test_duty_change_push.py).
"""
import json
import os
import sys

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

import app as A

TODAY = '2026-07-20'          # Montag (fixe Referenz für die Gate-Unit-Tests)
YESTERDAY = '2026-07-19'
TOMORROW = '2026-07-21'


def _hb_today():
    """DER Tages-Key, auf den die PRODUKTION ankert — Homebase-lokal.

    ZEITZONEN-FEHLERKLASSE (Task #13, rot am 29.07. unter Maschinen-TZ PDT):
    `take_roster_snapshot` leitet sein „heute" aus
    `_airport_local_now(_profile_homebase_cached(token) or 'FRA')` ab; die
    Fixtures unten pinnen die Homebase auf FRA, also Europe/Berlin. Die Tests
    bauten ihre Tages-Keys dagegen aus `date.today()` — der MASCHINEN-Zeitzone.
    Zwischen ~22:00 UTC und dem lokalen Mitternachtssprung (in PDT ein ~9-h-
    Fenster) zeigten beide Uhren auf VERSCHIEDENE Kalendertage: der Test schrieb
    den 29.07., die Produktion verglich gegen den 30.07. und stufte den Tag
    korrekt als Vergangenheit ein — `_roster_change_is_past` tat also genau das
    Richtige, der Test fragte das Falsche ab. Mit dem Datumssprung heilte es von
    selbst; die Wurzel blieb.

    Deshalb baut diese Datei ihre Tages-Keys ab jetzt aus DERSELBEN Quelle wie
    die Produktion. Kein Einfrieren der Uhr: die Gates sollen echt gegen die
    laufende Wanduhr geprüft werden — nur eben gegen DIE RICHTIGE."""
    return A._airport_local_now('FRA').date()


def _sector(flight='LH 440', frm='FRA', to='IAH',
            dep='2026-07-22T08:00:00Z', arr='2026-07-22T18:30:00Z'):
    return {'flight': flight, 'from': frm, 'to': to,
            'dep_iso': dep, 'arr_iso': arr}


# ══════════════════════════════════════════════════════════════════════════════
# Diff-Formatter: _roster_change_push_line / _roster_changes_push_body
# ══════════════════════════════════════════════════════════════════════════════
def test_push_line_added_names_flight_and_route():
    ch = {'kind': 'added', 'datum': '2026-07-22',
          'new': {'ical_sectors': [_sector()], 'routing': 'FRA-IAH'}}
    assert A._roster_change_push_line(ch) == 'LH440 FRA-IAH neu'
    # 2026-07-22 ist ein Mittwoch.
    assert A._roster_changes_push_body([ch]) == 'Mi 22.07: LH440 FRA-IAH neu'


def test_push_line_added_without_sectors_falls_back():
    ch = {'kind': 'added', 'datum': '2026-07-22', 'new': {'routing': 'FRA-IAH'}}
    assert A._roster_change_push_line(ch) == 'FRA-IAH neu'
    assert A._roster_change_push_line({'kind': 'added', 'new': {}}) == 'Neuer Dienst'


def test_push_line_removed():
    ch = {'kind': 'removed', 'datum': '2026-07-23',
          'old': {'ical_sectors': [_sector()]}}
    # 2026-07-23 ist ein Donnerstag.
    assert A._roster_changes_push_body([ch]) == 'Do 23.07: Dienst entfernt'


def test_push_line_briefing_time_change():
    ch = {'kind': 'modified', 'datum': TOMORROW,
          'old': {'routing': 'FRA-JFK', 'reader_facts': {'start_time': '09:40'}},
          'new': {'routing': 'FRA-JFK', 'reader_facts': {'start_time': '10:15'}}}
    assert A._roster_change_push_line(ch) == 'Briefing 09:40 → 10:15'
    assert A._roster_changes_push_body([ch]) == 'Di 21.07: Briefing 09:40 → 10:15'


def test_push_line_route_change_beats_times():
    ch = {'kind': 'modified', 'datum': TOMORROW,
          'old': {'routing': 'FRA-JFK', 'reader_facts': {'start_time': '09:40'}},
          'new': {'routing': 'FRA-MIA', 'reader_facts': {'start_time': '10:15'}}}
    assert A._roster_change_push_line(ch) == 'Route FRA-JFK → FRA-MIA'


def test_push_line_leg_departure_time_change_local():
    old = {'ical_sectors': [_sector(dep='2026-07-22T08:00:00Z')]}
    new = {'ical_sectors': [_sector(dep='2026-07-22T08:30:00Z')]}
    line = A._roster_change_push_line(
        {'kind': 'modified', 'old': old, 'new': new})
    # FRA-lokal (UTC+2 im Juli): 10:00 → 10:30.
    assert line == 'LH440 Abflug 10:00 → 10:30'


def test_push_body_counts_further_changes():
    chs = [{'kind': 'removed', 'datum': '2026-07-22', 'old': {}},
           {'kind': 'removed', 'datum': '2026-07-23', 'old': {}},
           {'kind': 'removed', 'datum': '2026-07-24', 'old': {}}]
    body = A._roster_changes_push_body(chs)
    assert body == 'Mi 22.07: Dienst entfernt (+2 weitere)'


def test_push_body_capped_at_120_chars():
    long_route = '-'.join(['FRA', 'JFK', 'MIA', 'GRU', 'EZE', 'SCL'] * 6)
    ch = {'kind': 'modified', 'datum': '2026-07-22',
          'old': {'routing': long_route}, 'new': {'routing': long_route + '-BOG'}}
    body = A._roster_changes_push_body([ch])
    assert len(body) <= 120 and body.endswith('…')


def test_push_body_never_throws_on_garbage():
    assert isinstance(A._roster_changes_push_body(None), str)
    assert isinstance(A._roster_changes_push_body([{'kind': 'modified'}]), str)
    assert isinstance(A._roster_change_push_line({}), str)
    assert A._roster_change_push_line(
        {'kind': 'modified', 'old': None, 'new': None}) == 'Dienst geändert'


# ══════════════════════════════════════════════════════════════════════════════
# Gate 1: Vergangenheit (_roster_change_is_past)
# ══════════════════════════════════════════════════════════════════════════════
def test_past_gate_yesterday_is_past():
    assert A._roster_change_is_past({'datum': YESTERDAY}, TODAY) is True


def test_past_gate_today_and_future_are_not_past():
    assert A._roster_change_is_past({'datum': TODAY}, TODAY) is False
    assert A._roster_change_is_past({'datum': TOMORROW}, TODAY) is False


def test_past_gate_defensive_on_missing_data():
    assert A._roster_change_is_past({}, TODAY) is False
    assert A._roster_change_is_past({'datum': YESTERDAY}, None) is False
    assert A._roster_change_is_past(None, TODAY) is False


# ══════════════════════════════════════════════════════════════════════════════
# Gate 2: Pickup-Rauschen — jetzt Teil des EINEN Substanz-Gates
# (_roster_change_is_push_worthy → _rc_meaningfully_modified). Die früheren
# Einzelfunktionen _roster_change_is_pickup_prune / _..._is_blocktime_drift
# sind 2026-07-28 ersatzlos entfallen: drei Implementierungen derselben Frage
# widersprachen sich (Owner-Fall 30.07.).
# ══════════════════════════════════════════════════════════════════════════════
def _day_with_pickup(pickup_marker='Pickup 1330', start='13:30',
                     dep='2026-07-22T12:00:00Z'):
    return {'datum': '2026-07-22', 'klass': 'Flug', 'routing': 'FRA-IAH',
            'marker': pickup_marker,
            'ical_sectors': [_sector(dep=dep)],
            'reader_facts': {'start_time': start, 'end_time': '19:00',
                             'layover_ort': 'IAH'}}


def _mod(old, new):
    return {'kind': 'modified', 'datum': '2026-07-22', 'old': old, 'new': new}


def _kein_eintrag(old, new, now=None):
    """OWNER-REGEL 2026-07-29: die Änderung erzeugt WEDER einen Verlauf-/
    pending-Eintrag NOCH einen Push — sie existiert schlicht nicht mehr als
    Change. Vorher galt hier „Verlauf ja, Push nein"."""
    datum = str((old or {}).get('datum') or (new or {}).get('datum') or '')[:10]
    assert A._rc_duty_substance_changed(old, new, now=now) is False
    assert A._compute_roster_diff([old], [new], today=datum, now=now) == []
    assert A._roster_change_is_push_worthy(
        {'kind': 'modified', 'datum': datum, 'old': old, 'new': new},
        now=now) is False


def _echter_eintrag(old, new, now=None):
    """Gegenprobe: „big change" → genau EIN 'modified'-Eintrag, und er pusht."""
    datum = str((old or {}).get('datum') or (new or {}).get('datum') or '')[:10]
    d = A._compute_roster_diff([old], [new], today=datum, now=now)
    assert len(d) == 1 and d[0]['kind'] == 'modified', d
    assert A._roster_change_is_push_worthy(
        {'kind': 'modified', 'datum': datum, 'old': old, 'new': new},
        now=now) is True
    return d


def test_pickup_abbau_ist_kein_push():
    # LH räumt die PU-Zeit ab: Marker verliert 'Pickup 1330', Start fällt von
    # der Pickup- (13:30) auf die Briefing-Zeit (14:30) zurück — sonst nichts.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_pickup_abbau_mit_abflug_shift_erzeugt_keinen_eintrag():
    # GATE 4 (2026-07-28): PU-Abbau + 1 h Abflug-Shift bei IDENTISCHER
    # Leg-Struktur ist eine reine Zeit-Änderung. Bis 28.07. hieß das „Verlauf
    # ja, Push nein" — seit dem Owner-Entscheid 2026-07-29 („Einfach nur big
    # changes", Screenshot Build 246) entsteht gar kein Eintrag mehr.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30',
                           dep='2026-07-22T13:00:00Z')     # Leg verschoben
    assert A._rc_meaningfully_modified(old, new) is True
    _kein_eintrag(old, new)


def test_pickup_abbau_mit_endzeit_pflege_bleibt_still():
    # `end_time` ist seit 2026-07-28 gar keine Substanz mehr (LH pflegt sie
    # nach jeder Landung) — zusammen mit dem PU-Abbau also weiterhin still.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='', start='14:30')
    new['reader_facts']['end_time'] = '20:15'
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False
    # GEÄNDERT 2026-07-29 (Phantom-Klasse (b)): ein anderer Layover-Ort BEI
    # unveränderten Sektoren ist ein Derivat der Leg-Reihenfolge, keine
    # Substanz. Substanz ist er nur noch, wenn die Sektoren ihn tragen …
    lay = _day_with_pickup(pickup_marker='', start='14:30')
    lay['reader_facts'] = dict(lay['reader_facts'], layover_ort='BOS')
    assert A._roster_change_is_push_worthy(_mod(old, lay)) is False
    lay['ical_sectors'] = [_sector(to='BOS')]
    assert A._roster_change_is_push_worthy(_mod(old, lay)) is True
    # … oder wenn eine Seite gar keine Sektoren hat (Feeds ohne ical_sectors).
    ohne_a = {k: v for k, v in old.items() if k != 'ical_sectors'}
    ohne_b = {k: v for k, v in old.items() if k != 'ical_sectors'}
    ohne_b['reader_facts'] = dict(old['reader_facts'], layover_ort='BOS')
    assert A._roster_change_is_push_worthy(_mod(ohne_a, ohne_b)) is True


def test_pickup_praesenz_flip_ist_still_pu_shift_erzeugt_keinen_eintrag():
    # OWNER-REGEL 2026-07-28: das Auftauchen/Verschwinden der PU-Zeit ohne
    # Zeitänderung am Dienst ist STILL (Flip-Flop-Signatur).
    ohne = _day_with_pickup(pickup_marker='', start='14:30')
    mit = _day_with_pickup()
    _kein_eintrag(ohne, mit)
    _kein_eintrag(mit, ohne)
    # Eine echte PU-VERSCHIEBUNG ≥ 5 min war bis 28.07. eine Verlauf-Änderung
    # ohne Push. Seit dem Owner-Entscheid 2026-07-29 („Einfach nur big
    # changes") ist sie auch aus der LISTE raus: reine Zeit, Struktur
    # identisch.
    verschoben = _day_with_pickup(pickup_marker='Pickup 1400', start='14:00')
    assert A._rc_meaningfully_modified(_day_with_pickup(), verschoben) is True
    _kein_eintrag(_day_with_pickup(), verschoben)
    # … und eine 3-Minuten-PU-Korrektur ist schon im Verlauf keine Änderung
    # (Toleranz _RC_TIME_TOL_MIN).
    assert A._roster_change_is_push_worthy(
        _mod(_day_with_pickup(),
             _day_with_pickup(pickup_marker='Pickup 1333',
                              start='13:33'))) is False


def test_push_worthy_defensive():
    assert A._roster_change_is_push_worthy({}) is True       # fail-open
    assert A._roster_change_is_push_worthy(None) is True
    assert A._roster_change_is_push_worthy(
        {'kind': 'added', 'datum': '2026-07-22', 'new': {}}) is False


def test_rc_pickup_hhmm_sources():
    assert A._rc_pickup_hhmm({'pickup': '9:05'}) == '09:05'
    assert A._rc_pickup_hhmm({'marker': 'Pickup 1430'}) == '14:30'
    assert A._rc_pickup_hhmm({'ical_summary': '09:30 LT Pickup HND'}) == '09:30'
    assert A._rc_pickup_hhmm({'marker': 'Briefing 0900'}) == ''
    assert A._rc_pickup_hhmm(None) == ''


# ══════════════════════════════════════════════════════════════════════════════
# Gate 3: Blockzeiten-Drift (Florian 2026-07-24) — als Regel im Substanz-Gate
# ══════════════════════════════════════════════════════════════════════════════
def test_blocktime_drift_end_time_only_not_pushworthy():
    # LH pflegt Blockzeiten: Legs/PU/Briefing/erster Abflug identisch, nur das
    # Dienst-Ende driftet um Minuten → kein Push.
    old = _day_with_pickup()
    new = _day_with_pickup()
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:12')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_arrival_iso_drift_not_pushworthy():
    # Nur die Ankunfts-ISO des Legs driftet (Struktur + Abflug gleich).
    old = _day_with_pickup()
    new = _day_with_pickup()
    new['ical_sectors'] = [_sector(dep='2026-07-22T12:00:00Z',
                                   arr='2026-07-22T18:47:00Z')]
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:17')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_first_departure_change_erzeugt_keinen_eintrag():
    # Der Abflug verschiebt sich um 30 min. Bis 28.07.: Verlauf ja, Push nein.
    # Seit dem Owner-Entscheid 2026-07-29 (das LH454-Ping-Pong stand als
    # „Abflug 10:25 → 10:55" in der Liste) fällt auch der Eintrag weg —
    # < 3 h, Struktur identisch.
    old = _day_with_pickup()
    new = _day_with_pickup(dep='2026-07-22T12:30:00Z')
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:30')
    assert A._rc_meaningfully_modified(old, new) is True
    _kein_eintrag(old, new)


def test_departure_drift_unter_toleranz_bleibt_still():
    # 3 Minuten Abflug-Korrektur = Zeitenpflege, kein Push (Owner-Regel:
    # erst ab 5 min). Vorher feuerte JEDE Minute.
    old = _day_with_pickup()
    new = _day_with_pickup(dep='2026-07-22T12:03:00Z')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_pickup_change_erzeugt_keinen_eintrag():
    # Wie oben, nur mit PU-Verschiebung statt Abflug — seit 2026-07-29 weder
    # Eintrag noch Push.
    old = _day_with_pickup()
    new = _day_with_pickup(pickup_marker='Pickup 1400', start='14:00')
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:12')
    assert A._rc_meaningfully_modified(old, new) is True
    _kein_eintrag(old, new)


def test_blocktime_drift_route_change_is_pushworthy():
    old = _day_with_pickup()
    new2 = _day_with_pickup()
    new2['routing'] = 'FRA-MIA'                            # Route geändert
    # GEÄNDERT 2026-07-29: das routing-FELD allein reicht nicht mehr, wenn
    # beide Seiten Sektoren tragen — dann ist es aus ihnen abgeleitet und erbt
    # jede Rotation. Die echte Streckenänderung steht in den Sektoren.
    assert A._roster_change_is_push_worthy(_mod(old, new2)) is False
    new2['ical_sectors'] = [_sector(to='MIA')]
    assert A._roster_change_is_push_worthy(_mod(old, new2)) is True


def test_briefing_drift_bei_stehender_pickup_bleibt_still():
    # P7-Entscheid bewahrt: steht die PU-Zeit, ist SIE die tragende Zeit —
    # eine Briefing-Minutenpflege darunter ist kein Push.
    old = _day_with_pickup()
    new = _day_with_pickup(start='14:00')
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False


def test_blocktime_drift_leg_structure_change_is_pushworthy():
    old = _day_with_pickup()
    new = _day_with_pickup()
    new['ical_sectors'] = [_sector(flight='LH 441')]       # andere Flugnummer
    assert A._roster_change_is_push_worthy(_mod(old, new)) is True


def test_endzeit_pflege_ohne_legs_bleibt_still():
    # Loch B: auch OHNE Legs (Standby/Boden) ist reine end_time-Pflege still.
    old = {'datum': '2026-07-22', 'klass': 'Standby',
           'reader_facts': {'start_time': '09:00', 'end_time': '17:00'}}
    new = dict(old, reader_facts={'start_time': '09:00', 'end_time': '18:00'})
    assert A._roster_change_is_push_worthy(_mod(old, new)) is False
    # Der Standby-BEGINN-Shift stand bis 28.07. im Verlauf (ohne Push). Seit
    # dem Owner-Entscheid 2026-07-29 ist auch er aus der Liste raus: der Dienst
    # selbst (Standby, kein Flug) ist derselbe.
    shift = dict(old, reader_facts={'start_time': '11:00', 'end_time': '18:00'})
    assert A._rc_meaningfully_modified(old, shift) is True
    _kein_eintrag(old, shift)


# ══════════════════════════════════════════════════════════════════════════════
# Gate 4: „nur die DIENST-Substanz zählt" (Owner-Entscheid 2026-07-28,
#         auf den VERLAUF ausgeweitet 2026-07-29)
#
# Owner, wörtlich: „Das Einzige, was aufpoppen darf, ist wirklich, wenn ich einen
# komplett neuen Flug habe — keine Verspätungen, keine Gate-Wechsel, keine
# Briefing-/Abflugzeit-Verschiebungen. Uns interessiert nur, ob sich der DIENST
# geändert hat: wenn ich San Francisco hatte und jetzt LA habe, ist das wichtig.
# Oder eine riesige Verspätung (~4 h), WENN ich meinen Dienst noch nicht
# angetreten habe."
#
# Am 28.07. blieb der VERLAUF bewusst vollständig, nur der Push wurde gefiltert.
# Am 29.07. hat der Owner das nach dem Screenshot von Build 246 explizit
# gekippt („Das sollte nicht mal aufpoppen. Das ist nicht wichtig!!!! Einfach
# nur big changes.") — dieselbe Regel gilt jetzt für Liste/Banner/Badge.
# ══════════════════════════════════════════════════════════════════════════════
_G4_DAY = '2026-08-02'


def _g4_day(flight='LH454', frm='FRA', to='SFO', dep='2026-08-02T09:00:00Z',
            arr='2026-08-02T20:00:00Z', start='08:00', layover='SFO',
            marker='08:00 LT Briefing FRA', klass='Flug'):
    return {'datum': _G4_DAY, 'klass': klass, 'routing': f'{frm}-{to}',
            'marker': marker,
            'ical_sectors': [{'flight': flight, 'from': frm, 'to': to,
                              'dep_iso': dep, 'arr_iso': arr}],
            'reader_facts': {'start_time': start, 'end_time': '20:30',
                             'layover_ort': layover}}


def _g4_mod(old, new):
    return {'kind': 'modified', 'datum': _G4_DAY, 'old': old, 'new': new}


# „Jetzt"-Zeitpunkte relativ zum Dienstbeginn (08:00 FRA-lokal = 06:00 UTC).
_G4_VOR_DIENST = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
_G4_IM_DIENST = datetime(2026, 8, 2, 7, 0, tzinfo=timezone.utc)


def test_gate4_briefing_shift_erzeugt_keinen_eintrag():
    # Der Forum-Fall („Sa 08.08: Briefing 00:35 → 01:05"): Struktur identisch,
    # nur die Meldezeit rutscht. Bis 28.07. „Verlauf ja, Push nein" — seit dem
    # Owner-Entscheid 2026-07-29 gar kein Eintrag mehr.
    old = _g4_day(start='00:35', marker='00:35 LT Briefing FRA')
    new = _g4_day(start='01:05', marker='01:05 LT Briefing FRA')
    assert A._rc_meaningfully_modified(old, new) is True
    _kein_eintrag(old, new, now=_G4_VOR_DIENST)


def test_gate4_abflug_shift_unter_drei_stunden_erzeugt_keinen_eintrag():
    # „LH454 Abflug 10:55 → 10:25" — 30 min, identische Legs → still, egal ob
    # der Dienst schon läuft oder nicht.
    old = _g4_day(dep='2026-08-02T08:55:00Z')
    new = _g4_day(dep='2026-08-02T08:25:00Z')
    assert A._rc_meaningfully_modified(old, new) is True
    for _now in (_G4_VOR_DIENST, _G4_IM_DIENST):
        _kein_eintrag(old, new, now=_now)


def test_gate4_pingpong_erzeugt_null_eintraege():
    # DER OWNER-SCREENSHOT (Build 246, 29.07.): „Abflug LH454: 10:25 → 10:55 ·
    # Ankunft 12:40 → 13:10" und direkt darunter dieselbe Änderung rückwärts.
    # Beide Richtungen sind reine Zeit → NULL Einträge, NULL Pushes. Es braucht
    # KEINE zusätzliche Dedup-Logik.
    a = _g4_day(dep='2026-08-02T08:55:00Z', arr='2026-08-02T20:00:00Z')
    b = _g4_day(dep='2026-08-02T08:25:00Z', arr='2026-08-02T19:30:00Z')
    _kein_eintrag(a, b, now=_G4_VOR_DIENST)
    _kein_eintrag(b, a, now=_G4_VOR_DIENST)
    # Über einen kompletten Ping-Pong-Zyklus hinweg entsteht KEIN einziger
    # pending-Eintrag — genau das, was der Owner in der Liste gesehen hat.
    zustand = a
    for naechster in (b, a, b, a, b, a):
        assert A._compute_roster_diff([zustand], [naechster], today=_G4_DAY,
                                      now=_G4_VOR_DIENST) == []
        zustand = naechster
    # … und auch als 4-h-Ping-Pong (Ausnahme greift) bleibt es symmetrisch:
    # beide Richtungen sind dann ECHTE Einträge, den Push-Sturm fängt die
    # Flip-Flop-Hysterese ab.
    weit = _g4_day(dep='2026-08-02T13:00:00Z')
    _echter_eintrag(a, weit, now=_G4_VOR_DIENST)
    _echter_eintrag(weit, a, now=_G4_VOR_DIENST)


def test_gate4_gate_und_blockzeiten_erzeugen_keinen_eintrag():
    # Ankunft/Blockzeit/Dienstende — die klassische LH-Zeitenpflege.
    old = _g4_day()
    new = _g4_day(arr='2026-08-02T20:47:00Z')
    new['reader_facts'] = dict(new['reader_facts'], end_time='21:17')
    # Das ist schon unterhalb von `_rc_meaningfully_modified` still.
    assert A._rc_meaningfully_modified(old, new) is False
    _kein_eintrag(old, new, now=_G4_VOR_DIENST)


def test_gate4_anderes_ziel_erzeugt_genau_einen_eintrag():
    # „Ich hatte San Francisco und jetzt habe ich LA" — genau das soll poppen,
    # und genau das bleibt seit 2026-07-29 als EINZIGE Sorte in der Liste.
    old = _g4_day(to='SFO', layover='SFO')
    new = _g4_day(to='LAX', layover='LAX')
    _echter_eintrag(old, new, now=_G4_VOR_DIENST)
    # Auch wenn der Dienst schon läuft (Umleitung im Dienst = harte Info).
    _echter_eintrag(old, new, now=_G4_IM_DIENST)


def test_gate4_andere_flugnummer_und_neuer_leg_erzeugen_je_einen_eintrag():
    old = _g4_day()
    _echter_eintrag(old, _g4_day(flight='LH456'), now=_G4_VOR_DIENST)
    zwei_legs = _g4_day()
    zwei_legs['ical_sectors'] = list(zwei_legs['ical_sectors']) + [
        {'flight': 'LH455', 'from': 'SFO', 'to': 'FRA',
         'dep_iso': '2026-08-03T22:00:00Z', 'arr_iso': '2026-08-04T08:00:00Z'}]
    _echter_eintrag(old, zwei_legs, now=_G4_VOR_DIENST)


def test_gate4_routing_regel_gilt_nur_ohne_sektoren():
    # PRÄZISIERT 2026-07-29 (Phantom-Klasse (b)): Regel 3 (routing/layover_ort)
    # ist laut Docstring dafür da, „die Zielinformation bei Feeds OHNE
    # ical_sectors" zu tragen. Liegen auf beiden Seiten Sektoren, ist sie ein
    # DERIVAT davon — und erbt jede Reihenfolge-Rotation der Quelle
    # (AT-687E2AFA61B942CF: layover_ort MUC↔YVR bei identischen Legs).
    # (1) Beidseitig Sektoren, identische Legs, nur layover_ort kippt → still.
    old = _g4_day()
    lay = _g4_day()
    lay['reader_facts'] = dict(lay['reader_facts'], layover_ort='OAK')
    _kein_eintrag(old, lay, now=_G4_VOR_DIENST)
    # dito für das routing-Feld allein.
    rt = _g4_day()
    rt['routing'] = 'FRA-SFO-OAK'
    _kein_eintrag(old, rt, now=_G4_VOR_DIENST)
    # (2) Routing-Feld ohne Sektoren (Feeds ohne ical_sectors) → weiter Substanz,
    #     der dokumentierte Zweck der Regel bleibt erhalten.
    o = {'datum': _G4_DAY, 'klass': 'Flug', 'routing': 'FRA-SFO'}
    n = {'datum': _G4_DAY, 'klass': 'Flug', 'routing': 'FRA-LAX'}
    _echter_eintrag(o, n, now=_G4_VOR_DIENST)
    # (3) Auch einseitig degradiert (neu ohne Sektoren) greift die Regel noch.
    ohne = dict(_g4_day(), ical_sectors=[], routing='FRA-LAX')
    _echter_eintrag(old, ohne, now=_G4_VOR_DIENST)
    # (4) ECHTER Zielwechsel bleibt unberührt — den trägt Regel 2 (Sektoren).
    _echter_eintrag(_g4_day(to='DEL', layover='DEL'),
                    _g4_day(to='BOM', layover='BOM'), now=_G4_VOR_DIENST)


# ══════════════════════════════════════════════════════════════════════════════
# PHANTOM-KLASSE (b) 2026-07-29: die REIHENFOLGE der Legs ist keine Substanz
#
# `_rc_sector_structure` gab eine GEORDNETE Liste zurück, das Gate verglich
# `sa_ != sb_`. Konkurrierende Importer (iCal-Feed, LH-FlightOps-ICS, PDF)
# legen dieselben Legs in unterschiedlicher Reihenfolge ab → Phantom-Änderung
# bei jedem Quellenwechsel. Live-Belege vom 29.07.:
#   · AT-7634A16FCF5A498B, 15.07.: identische drei Legs, nur rotiert
#     (MUC-PMI-STR-PMI ↔ STR-PMI-MUC-PMI-STR),
#   · AT-687E2AFA61B942CF: Pickup-Marker vor/nach Layover vertauscht, mit
#     layover_ort MUC↔YVR als Folgefehler.
# Fix: `sorted()` im Fingerprint (Identität = Flugnummer + Stationen) +
# Regel 3 nur noch ohne beidseitige Sektoren.
# ══════════════════════════════════════════════════════════════════════════════
_ROT_DAY = '2026-08-05'


def _leg(flight, frm, to, dep):
    return {'flight': flight, 'from': frm, 'to': to,
            'dep_iso': f'{_ROT_DAY}T{dep}:00Z',
            'arr_iso': f'{_ROT_DAY}T{dep}:00Z'}


_ROT_LEGS = [_leg('LH100', 'MUC', 'PMI', '06:00'),
             _leg('LH101', 'PMI', 'STR', '09:00'),
             _leg('LH102', 'STR', 'PMI', '12:00')]


def _rot_day(legs, routing='MUC-PMI-STR-PMI', layover='PMI'):
    return {'datum': _ROT_DAY, 'klass': 'Flug', 'routing': routing,
            'marker': '05:00 LT Briefing MUC',
            'ical_sectors': list(legs),
            'reader_facts': {'start_time': '05:00', 'end_time': '13:00',
                             'layover_ort': layover}}


_ROT_NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def test_sector_structure_ist_reihenfolge_invariant():
    a = _rot_day(_ROT_LEGS)
    b = _rot_day([_ROT_LEGS[2], _ROT_LEGS[0], _ROT_LEGS[1]])
    assert A._rc_sector_structure(a) == A._rc_sector_structure(b)
    assert A._rc_sector_structure(a) == sorted(A._rc_sector_structure(a))
    # … und damit auch die Hysterese-Signatur (sonst pushte jede Rotation neu).
    assert A._rc_state_sig(a) == A._rc_state_sig(b)


def test_rotierte_identische_legs_erzeugen_keinen_eintrag():
    # AT-7634A16FCF5A498B, 15.07.: dieselben drei Legs, andere Array-Reihenfolge.
    a = _rot_day(_ROT_LEGS)
    b = _rot_day([_ROT_LEGS[2], _ROT_LEGS[0], _ROT_LEGS[1]])
    assert A._rc_meaningfully_modified(a, b) is False
    _kein_eintrag(a, b, now=_ROT_NOW)
    _kein_eintrag(b, a, now=_ROT_NOW)


def test_rotation_mit_layover_folgefehler_bleibt_still():
    # AT-687E2AFA61B942CF: Pickup-Marker vor/nach Layover vertauscht → die
    # Quelle leitet daraus ein anderes routing/layover_ort ab. Solange die
    # SEKTOREN beidseitig identisch sind, ist das ein Derivat der Rotation.
    a = _rot_day(_ROT_LEGS, routing='MUC-PMI-STR-PMI', layover='MUC')
    b = _rot_day([_ROT_LEGS[1], _ROT_LEGS[2], _ROT_LEGS[0]],
                 routing='STR-PMI-MUC-PMI-STR', layover='YVR')
    _kein_eintrag(a, b, now=_ROT_NOW)


def test_echter_leg_tausch_bleibt_ein_eintrag_trotz_sortierung():
    # Gegenprobe DEL→BOM: das Ziel wechselt wirklich → Eintrag + Push. Die
    # Sortierung darf einen echten Tausch NIE verschlucken.
    a = _rot_day(_ROT_LEGS)
    b = _rot_day([_ROT_LEGS[0], _ROT_LEGS[1],
                  _leg('LH102', 'STR', 'BOM', '12:00')])
    _echter_eintrag(a, b, now=_ROT_NOW)
    # Auch ein zusätzlicher/fehlender Leg bleibt sichtbar.
    _echter_eintrag(a, _rot_day(_ROT_LEGS[:2]), now=_ROT_NOW)
    # … und ein Flugnummern-Tausch bei gleichen Stationen ebenso.
    _echter_eintrag(a, _rot_day([_ROT_LEGS[0], _ROT_LEGS[1],
                                 _leg('LH999', 'STR', 'PMI', '12:00')]),
                    now=_ROT_NOW)


def test_rotation_am_endpoint_erzeugt_weder_pending_noch_push(tmp_path):
    d = (_hb_today() + timedelta(days=4)).isoformat()

    def _at(datum, legs, layover):
        day = _rot_day(legs, layover=layover)
        day['datum'] = datum
        day['ical_sectors'] = [dict(s, dep_iso=s['dep_iso'].replace(_ROT_DAY,
                                                                   datum),
                                    arr_iso=s['arr_iso'].replace(_ROT_DAY,
                                                                 datum))
                               for s in legs]
        return day

    r, push, changes_file = _post(
        tmp_path,
        old=[_at(d, _ROT_LEGS, 'MUC')],
        new=[_at(d, [_ROT_LEGS[2], _ROT_LEGS[0], _ROT_LEGS[1]], 'YVR')])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0


def test_gate4_dienst_kommt_und_geht_bleibt_ein_eintrag():
    # Flug wird Standby (Flug-Beleg verschwindet in einen dokumentierten Tag).
    old = _g4_day()
    standby = {'datum': _G4_DAY, 'klass': 'Standby', 'routing': '',
               'reader_facts': {'start_time': '08:00', 'end_time': '16:00'}}
    _echter_eintrag(old, standby, now=_G4_VOR_DIENST)
    # Frei → Dienst.
    frei = {'datum': _G4_DAY, 'klass': 'FREI'}
    _echter_eintrag(frei, standby, now=_G4_VOR_DIENST)


def test_gate4_riesenverspaetung_vor_dienstantritt_erzeugt_einen_eintrag():
    # 4 h später, Crew ist noch zu Hause → das ist die eine Zeit-Änderung,
    # die zählt: die Huge-Delay-Ausnahme bleibt auch nach dem Owner-Entscheid
    # vom 29.07. ein ECHTER Eintrag (nicht nur ein Push).
    old = _g4_day(dep='2026-08-02T09:00:00Z')
    new = _g4_day(dep='2026-08-02T13:00:00Z')
    _echter_eintrag(old, new, now=_G4_VOR_DIENST)


def test_gate4_riesenverspaetung_nach_dienstantritt_bleibt_still():
    # Dieselbe Verschiebung, aber der Dienst läuft schon (07:00 UTC >
    # Dienstbeginn 06:00 UTC) → die Crew steht am Flughafen und erfährt es dort.
    # Seit 2026-07-29 heißt „still" auch hier: kein Eintrag.
    old = _g4_day(dep='2026-08-02T09:00:00Z')
    new = _g4_day(dep='2026-08-02T13:00:00Z')
    _kein_eintrag(old, new, now=_G4_IM_DIENST)


def test_gate4_riesenverspaetung_schwelle_ist_180_minuten():
    old = _g4_day(dep='2026-08-02T09:00:00Z')
    knapp = _g4_day(dep='2026-08-02T11:59:00Z')          # 179 min
    genau = _g4_day(dep='2026-08-02T12:00:00Z')          # 180 min
    _kein_eintrag(old, knapp, now=_G4_VOR_DIENST)
    _echter_eintrag(old, genau, now=_G4_VOR_DIENST)


def test_gate4_grosse_vorverlegung_erzeugt_ebenfalls_einen_eintrag():
    # 3 h FRÜHER ist für die Anreise noch kritischer als 3 h später.
    old = _g4_day(dep='2026-08-02T13:00:00Z', start='12:00',
                  marker='12:00 LT Briefing FRA')
    new = _g4_day(dep='2026-08-02T09:00:00Z', start='12:00',
                  marker='12:00 LT Briefing FRA')
    _echter_eintrag(old, new, now=_G4_VOR_DIENST)


def test_gate4_added_und_removed_unveraendert():
    # Gate 4 fasst added/removed NICHT an — weder im Push noch (seit
    # 2026-07-29) im Diff.
    assert A._roster_change_is_push_worthy(
        {'kind': 'added', 'datum': _G4_DAY, 'new': _g4_day()}) is True
    assert A._roster_change_is_push_worthy(
        {'kind': 'removed', 'datum': _G4_DAY, 'old': _g4_day()}) is True
    assert A._roster_change_is_push_worthy(
        {'kind': 'added', 'datum': _G4_DAY, 'new': {}}) is False
    # Diff-Ebene: ein neuer bzw. entfallener Flugtag bleibt ein Eintrag.
    heute = _G4_DAY
    add = A._compute_roster_diff([], [_g4_day()], today=heute)
    assert len(add) == 1 and add[0]['kind'] == 'added'
    rem = A._compute_roster_diff([_g4_day()], [], today=heute)
    assert len(rem) == 1 and rem[0]['kind'] == 'removed'


def test_gate4_defensiv():
    assert A._rc_duty_substance_changed(None, None) is False
    assert A._rc_duty_substance_changed({}, {}) is False
    assert A._rc_huge_delay_before_duty({}, {}) is False
    assert A._rc_huge_delay_before_duty(_g4_day(), {'ical_sectors': []}) is False
    # Naives „now" wird als UTC gelesen (kein TypeError beim Vergleich).
    assert A._roster_change_is_push_worthy(
        _g4_mod(_g4_day(dep='2026-08-02T09:00:00Z'),
                _g4_day(dep='2026-08-02T13:00:00Z')),
        now=datetime(2026, 8, 1, 12, 0)) is True


def test_rc_duty_start_utc_quellen():
    # 08:00 FRA-lokal (UTC+2 im August) = 06:00 UTC.
    assert A._rc_duty_start_utc(_g4_day()) == datetime(
        2026, 8, 2, 6, 0, tzinfo=timezone.utc)
    # Ohne start_time greift der Abflug als spätestmöglicher Dienstbeginn.
    ohne = _g4_day(start='', marker='')
    ohne['reader_facts'] = {'end_time': '20:30'}
    assert A._rc_duty_start_utc(ohne) == datetime(
        2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    # Station außerhalb Europas: 22:00 lokal SFO (UTC-7) = 05:00 UTC am Folgetag.
    sfo = _g4_day(frm='SFO', to='FRA', start='22:00',
                  marker='22:00 LT Briefing SFO',
                  dep='2026-08-03T06:00:00Z', arr='2026-08-03T16:00:00Z')
    assert A._rc_duty_start_utc(sfo) == datetime(
        2026, 8, 3, 5, 0, tzinfo=timezone.utc)
    # Implausible Projektion (Beginn NACH dem Abflug) → Abflug gewinnt.
    spaet = _g4_day(start='23:00', marker='23:00 LT Briefing FRA')
    assert A._rc_duty_start_utc(spaet) == datetime(
        2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    assert A._rc_duty_start_utc(None) is None
    assert A._rc_duty_start_utc({}) is None


# ══════════════════════════════════════════════════════════════════════════════
# Endpoint: take_roster_snapshot — Substanz-Gates wirken auf EINTRAG und Push,
# das Vergangenheits-Gate nur auf den Push
# (Muster + Patches wie tests/test_duty_change_push.py::_snapshot_env)
# ══════════════════════════════════════════════════════════════════════════════
def _snapshot_env(tmp_path, old_tage):
    changes_file = tmp_path / 'roster_changes_test.json'
    push = MagicMock()
    return (
        patch.object(A, '_roster_snapshot_read',
                     return_value={'tage': old_tage} if old_tage else {}),
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


def _tag(datum, klass='Flug', routing='FRA-JFK'):
    return {'datum': datum, 'klass': klass, 'routing': routing}


def _post(tmp_path, old, new):
    p1, p2, p3, p4, p5, p6, p7, push, changes_file = _snapshot_env(tmp_path, old)
    with p1, p2, p3, p4, p5, p6, p7:
        client = A.app.test_client()
        r = client.post('/api/user/roster-snapshot/testtoken123',
                        json={'tage': new})
    return r, push, changes_file


def test_past_modified_landet_im_verlauf_statt_im_pending(tmp_path):
    # GEÄNDERT 2026-07-29 (Phantom-Klasse (a)): bis dahin landete ein
    # 'modified' an einem VERGANGENEN Tag zwar nicht im Push, aber sehr wohl
    # im pending — und damit im Badge. Fleet-Messung: 77 von 172 offenen
    # pending-Einträgen hingen an Tagen < heute. Jetzt: Verlauf ja, pending
    # nein, Push nein.
    d_past = (_hb_today() - timedelta(days=1)).isoformat()
    r, push, changes_file = _post(
        tmp_path,
        old=[_tag(d_past, routing='FRA-JFK')],
        new=[_tag(d_past, routing='FRA-MIA')])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 1     # der Change EXISTIERT …
    data = json.loads(changes_file.read_text())
    assert data['pending'] == []                  # … aber ohne Badge-Wirkung
    assert len(data['history']) == 1
    assert data['history'][0]['datum'] == d_past
    assert data['history'][0]['status'] == 'past_auto'
    assert push.call_count == 0                   # und KEIN Push


def test_heutiger_modified_bleibt_pending(tmp_path):
    # Gegenprobe: HEUTE ist nicht Vergangenheit. Der „heute, aber Dienst schon
    # beendet"-Zweig von `_roster_change_is_past` bleibt dem PUSH vorbehalten —
    # die Liste zeigt den heutigen Tag weiter als offen.
    d_today = _hb_today().isoformat()
    r, push, changes_file = _post(
        tmp_path,
        old=[_tag(d_today, routing='FRA-JFK')],
        new=[_tag(d_today, routing='FRA-MIA')])
    assert r.status_code == 200
    data = json.loads(changes_file.read_text())
    assert len(data['pending']) == 1 and data['pending'][0]['status'] == 'pending'
    assert data.get('history') in ([], None)


@pytest.mark.parametrize('zone', ['America/Los_Angeles', 'Pacific/Auckland',
                                  'UTC'])
def test_vergangenheits_gate_ist_unabhaengig_von_der_maschinen_zeitzone(
        tmp_path, zone):
    """DER PIN zu Task #13 (Muster: test_lh_mqtt / test_lhfo_quota_diet).

    Die Zell-Familie „gestern → Verlauf / heute → pending / morgen → pending"
    läuft unter FREMD gestellter Prozess-Zeitzone. `America/Los_Angeles` ist
    genau die Konstellation, die am 29.07. rot war (lokal 29., UTC 30.);
    `Pacific/Auckland` prüft die Gegenrichtung (lokal schon der Folgetag).

    Wäre irgendwo im Pfad — Produktion ODER Test — noch eine Maschinen-lokale
    Tages-Ableitung, kippte mindestens eine der drei Zellen. Der Tages-Key
    kommt aus `_hb_today()` (Homebase FRA), also aus derselben Quelle wie in
    `take_roster_snapshot`; die Uhr selbst bleibt echt."""
    import time as _time
    alt = os.environ.get('TZ')
    os.environ['TZ'] = zone
    try:
        _time.tzset()
    except AttributeError:                                   # pragma: no cover
        pytest.skip('tzset auf dieser Plattform nicht verfügbar')
    try:
        heute = _hb_today()
        faelle = {'gestern': (heute - timedelta(days=1)).isoformat(),
                  'heute': heute.isoformat(),
                  'morgen': (heute + timedelta(days=1)).isoformat()}
        for name, d in faelle.items():
            box = tmp_path / f'{zone.replace("/", "_")}_{name}'
            box.mkdir()
            r, push, changes_file = _post(
                box,
                old=[_tag(d, routing='FRA-JFK')],
                new=[_tag(d, routing='FRA-MIA')])
            assert r.status_code == 200, (zone, name)
            data = json.loads(changes_file.read_text())
            if name == 'gestern':
                assert data['pending'] == [], (zone, name)
                assert len(data['history']) == 1, (zone, name)
                assert data['history'][0]['status'] == 'past_auto'
                assert push.call_count == 0, (zone, name)
            else:
                assert len(data['pending']) == 1, (zone, name)
                assert data['pending'][0]['status'] == 'pending', (zone, name)
                assert data.get('history') in ([], None), (zone, name)
    finally:
        if alt is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = alt
        try:
            _time.tzset()
        except AttributeError:                               # pragma: no cover
            pass


def test_altbestand_vergangener_pendings_heilt_sich_selbst(tmp_path):
    # SELBSTHEILUNG: das Gate wirkt nur auf NEUE Diffs — die am 29.07.
    # gemessenen 75 offenen Vergangenheits-'modified' auf 40 Token würden sonst
    # ewig im Badge stehen. Jeder Snapshot räumt sie jetzt nach `history`, auch
    # wenn der aktuelle Diff LEER ist.
    d_past = (_hb_today() - timedelta(days=3)).isoformat()
    d_fut = (_hb_today() + timedelta(days=3)).isoformat()
    changes_file = tmp_path / 'roster_changes_test.json'
    changes_file.write_text(json.dumps({'pending': [
        {'datum': d_past, 'kind': 'modified', 'status': 'pending'},
        {'datum': d_past, 'kind': 'removed', 'status': 'pending'},
        {'datum': d_fut, 'kind': 'modified', 'status': 'pending'},
    ], 'history': []}))
    tag = _tag(d_fut)
    p1, p2, p3, p4, p5, p6, p7, push, _cf = _snapshot_env(tmp_path, [tag])
    with p1, p2, p3, patch.object(A, '_roster_changes_path',
                                  return_value=str(changes_file)), p5, p6, p7, \
            patch.object(A, '_sb_roster_changes_load', return_value=None), \
            patch.object(A, '_sb_roster_changes_upsert', return_value=False):
        r = A.app.test_client().post('/api/user/roster-snapshot/testtoken123',
                                     json={'tage': [tag]})
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0        # gar kein neuer Diff
    data = json.loads(changes_file.read_text())
    kinds = sorted((c['datum'], c['kind']) for c in data['pending'])
    assert kinds == [(d_past, 'removed'), (d_fut, 'modified')]
    assert [c['datum'] for c in data['history']] == [d_past]
    assert data['history'][0]['status'] == 'past_auto'
    assert push.call_count == 0


def test_past_added_und_removed_bleiben_pending(tmp_path):
    # Nur 'modified' wird archiviert. Ein NACHGETRAGENER oder GESTRICHENER
    # Dienst in der Vergangenheit ist für Logbuch/Steuer relevant und bleibt
    # eine offene Kenntnisnahme. ('added' meldet der Diff nur für heute…+10 d,
    # darum hier über 'removed' geprüft.)
    d_past = (_hb_today() - timedelta(days=1)).isoformat()
    r, push, changes_file = _post(tmp_path, old=[_tag(d_past)], new=[])
    assert r.status_code == 200
    data = json.loads(changes_file.read_text())
    assert len(data['pending']) == 1
    assert data['pending'][0]['kind'] == 'removed'
    assert push.call_count == 0                   # Push-Gate bleibt Vergangenheit


def test_pickup_flip_weder_verlauf_noch_push(tmp_path):
    # OWNER-REGEL 2026-07-28: Rauschen darf jetzt auch NICHT MEHR in den
    # Verlauf („Dienstplan → Verlauf zeigt immer wieder Änderungen").
    d = (_hb_today() + timedelta(days=1)).isoformat()
    old = dict(_day_with_pickup(), datum=d)
    new = dict(_day_with_pickup(pickup_marker='', start='14:30'), datum=d)
    r, push, changes_file = _post(tmp_path, old=[old], new=[new])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0


def test_blocktime_drift_weder_verlauf_noch_push(tmp_path):
    d = (_hb_today() + timedelta(days=2)).isoformat()
    old = dict(_day_with_pickup(), datum=d)
    new = dict(_day_with_pickup(), datum=d)
    new['reader_facts'] = dict(new['reader_facts'], end_time='19:20')
    r, push, changes_file = _post(tmp_path, old=[old], new=[new])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0


def test_mixed_past_and_future_pushes_only_future(tmp_path):
    d_past = (_hb_today() - timedelta(days=1)).isoformat()
    d_fut = (_hb_today() + timedelta(days=2)).isoformat()
    r, push, _cf = _post(
        tmp_path,
        old=[_tag(d_past, routing='FRA-JFK'), _tag(d_fut, routing='FRA-JFK')],
        new=[_tag(d_past, routing='FRA-MIA'), _tag(d_fut, routing='FRA-GRU')])
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 2
    assert push.call_count == 1
    kwargs = push.call_args.kwargs
    # Nach dem Filter bleibt genau EINE push-würdige Änderung → deren Datum
    # ist die roster_change_id, und der Body nennt sie konkret.
    assert kwargs['data']['roster_change_id'] == d_fut
    body = push.call_args.args[2]
    assert 'Route FRA-JFK → FRA-GRU' in body
    assert 'weitere' not in body


def _fut_flug_tag(datum, dep_hhmm='09:00', flight='LH454', to='SFO'):
    """Flugtag an `datum` (Zukunft) mit UTC-Abflug `dep_hhmm` — die Endpoint-
    Tests laufen auf der echten Wanduhr, darum ein zukünftiger Tag: der Dienst
    hat garantiert noch nicht begonnen."""
    return {'datum': datum, 'klass': 'Flug', 'routing': f'FRA-{to}',
            'marker': '08:00 LT Briefing FRA',
            'ical_sectors': [{'flight': flight, 'from': 'FRA', 'to': to,
                              'dep_iso': f'{datum}T{dep_hhmm}:00Z',
                              'arr_iso': f'{datum}T20:00:00Z'}],
            'reader_facts': {'start_time': '08:00', 'end_time': '20:30',
                             'layover_ort': to}}


def test_gate4_zeitshift_erzeugt_am_endpoint_gar_nichts(tmp_path):
    # KERN-INVARIANTE seit dem Owner-Entscheid 2026-07-29: ein reiner
    # Zeit-Shift erzeugt WEDER pending-Eintrag NOCH Push. Bis 28.07. stand hier
    # noch „Verlauf vollständig, nur der Push wird gefiltert" — der Owner hat
    # das nach dem Screenshot von Build 246 explizit gekippt.
    d = (_hb_today() + timedelta(days=3)).isoformat()
    r, push, changes_file = _post(
        tmp_path,
        old=[_fut_flug_tag(d, dep_hhmm='09:00')],
        new=[_fut_flug_tag(d, dep_hhmm='09:40')])       # 40 min später
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0
    assert json.loads(changes_file.read_text())['pending'] == []
    assert push.call_count == 0


def test_gate4_pingpong_erzeugt_null_verlaufseintraege_und_null_pushes(tmp_path):
    # Der Live-Fall aus dem Owner-Screenshot: 10:55 → 10:25 und 30 s später
    # zurück, beide Richtungen untereinander in der Liste. Jetzt: nichts.
    d = (_hb_today() + timedelta(days=3)).isoformat()
    a, b = _fut_flug_tag(d, dep_hhmm='08:55'), _fut_flug_tag(d, dep_hhmm='08:25')
    for old, new in ((a, b), (b, a)):
        r, push, changes_file = _post(tmp_path, old=[old], new=[new])
        assert r.status_code == 200
        assert r.get_json()['changes_count'] == 0
        assert json.loads(changes_file.read_text())['pending'] == []
        assert push.call_count == 0


def test_gate4_stille_zeitaenderung_wird_trotzdem_in_die_daten_uebernommen(tmp_path):
    # AUTO-ÜBERNAHME: nur der EINTRAG entfällt, die Zeit selbst muss im Roster
    # ankommen. Der Snapshot wird unabhängig vom Diff geschrieben → der neue
    # Stand (09:40) landet 1:1 in `_roster_snapshot_save`.
    d = (_hb_today() + timedelta(days=3)).isoformat()
    neu = _fut_flug_tag(d, dep_hhmm='09:40')
    saved = {}
    p1, p2, p3, p4, p5, p6, p7, push, changes_file = _snapshot_env(
        tmp_path, [_fut_flug_tag(d, dep_hhmm='09:00')])
    with p1, p3, p4, p5, p6, p7, patch.object(
            A, '_roster_snapshot_save',
            side_effect=lambda _t, payload: saved.update(payload) or True):
        r = A.app.test_client().post('/api/user/roster-snapshot/testtoken123',
                                     json={'tage': [neu]})
    assert r.status_code == 200
    assert r.get_json()['changes_count'] == 0            # kein Eintrag …
    assert saved['tage'] == [neu]                        # … aber die Daten sind da
    assert (saved['tage'][0]['ical_sectors'][0]['dep_iso']
            == f'{d}T09:40:00Z')


def test_gate4_riesenverspaetung_erzeugt_am_endpoint_einen_eintrag(tmp_path):
    # Die Huge-Delay-Ausnahme bleibt ein ECHTER Eintrag (+ Push).
    d = (_hb_today() + timedelta(days=3)).isoformat()
    r, push, changes_file = _post(
        tmp_path,
        old=[_fut_flug_tag(d, dep_hhmm='09:00')],
        new=[_fut_flug_tag(d, dep_hhmm='13:00')])       # 4 h später
    assert r.status_code == 200 and push.call_count == 1
    assert r.get_json()['changes_count'] == 1
    assert len(json.loads(changes_file.read_text())['pending']) == 1


def test_gate4_zielwechsel_erzeugt_am_endpoint_einen_eintrag(tmp_path):
    d = (_hb_today() + timedelta(days=3)).isoformat()
    r, push, changes_file = _post(
        tmp_path,
        old=[_fut_flug_tag(d, to='SFO')],
        new=[_fut_flug_tag(d, to='LAX')])
    assert r.status_code == 200 and push.call_count == 1
    assert r.get_json()['changes_count'] == 1
    pending = json.loads(changes_file.read_text())['pending']
    assert len(pending) == 1 and pending[0]['kind'] == 'modified'


def test_future_change_push_body_is_concrete(tmp_path):
    d_fut = (_hb_today() + timedelta(days=2)).isoformat()
    r, push, _cf = _post(
        tmp_path,
        old=[_tag(d_fut, routing='FRA-JFK')],
        new=[_tag(d_fut, routing='FRA-MIA')])
    assert r.status_code == 200 and push.call_count == 1
    body = push.call_args.args[2]
    assert body == (f'{A._rc_datum_label(d_fut)}: Route FRA-JFK → FRA-MIA')
    assert len(body) <= 120
    assert push.call_args.kwargs['category'] == 'DUTY_CHANGE'
