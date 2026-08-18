"""DH-Marker aus dem iCal-SUMMARY (Christopher Magin, 05.08./08.08.2026).

myTime schreibt Deadheads als „DH LH 122: FRA-MUC" — der Marker steht NUR im
Summary-Text. Die Flugnummern-Regex in `_build_ical_sectors` griff nur
„LH 122", der DH-Marker ging verloren und `dh` wurde nie gesetzt (das X-Prop
X-AEROX-DH liefert myTime nicht). Folge: iOS `isDeadhead` griff nie und die
MTV-Rechnung § 9 (4) a (Deadhead zählt zur Hälfte) stimmte nicht.

Token-Regel wie iOS `flightNumberLooksDeadhead`: „DH" nur als eigenständiges
Wort (oder „DEADHEAD" als Substring) — NICHT bei „DHA-JED" (IATA-Paar),
„DH8 400" (Dash 8) oder „DHL" (Carrier).
"""
import app as A


def _sector(summary):
    ev = {'summary': summary,
          'start_iso': '2026-08-05T06:10:00Z',
          'end_iso': '2026-08-05T07:15:00Z'}
    days = A._build_ical_sectors([ev])
    assert days, f'kein Sektor gebaut für {summary!r}'
    secs = list(days.values())[0]
    assert len(secs) == 1
    return secs[0]


def test_dh_token_before_flight_number_sets_dh():
    sec = _sector('DH LH 122: FRA-MUC')
    assert sec['flight'] == 'LH122'
    assert sec['from'] == 'FRA' and sec['to'] == 'MUC'
    assert sec.get('dh') is True


def test_plain_flight_has_no_dh_key():
    # Byte-identisch-Garantie: ohne DH darf der Key GAR NICHT existieren
    # (jsonb-Containment-Matching in lh_mqtt).
    sec = _sector('LH 474: MUC-YUL')
    assert 'dh' not in sec


def test_iata_pair_dha_jed_is_not_deadhead():
    # „DHA" ist ein Flughafen — das DH klebt am A, kein eigenständiges Token.
    sec = _sector('LH 1303: DHA-JED')
    assert 'dh' not in sec


def test_aircraft_type_dh8_is_not_deadhead():
    # Dash 8 („DH8 400") im Summary darf keinen Deadhead erzeugen.
    sec = _sector('OS 227: VIE-INN DH8 400')
    assert 'dh' not in sec


def test_dhl_carrier_is_not_deadhead():
    sec = _sector('DHL POSITIONING LH 8460: LEJ-FRA')
    # „DHL" enthält DH nur als Substring — kein eigenständiges Token.
    assert 'dh' not in sec


def test_deadhead_substring_sets_dh():
    sec = _sector('DEADHEAD LH103: FRA-MUC')
    assert sec.get('dh') is True


def test_x_prop_ax_dh_still_works():
    # Der alte X-AEROX-DH-Pfad (LH-Gratis-Ernte) bleibt unangetastet.
    ev = {'summary': 'LH 474: MUC-YUL',
          'start_iso': '2026-08-05T06:10:00Z',
          'end_iso': '2026-08-05T14:15:00Z',
          'ax_dh': True}
    days = A._build_ical_sectors([ev])
    assert list(days.values())[0][0].get('dh') is True
