"""Betriebstag-Gate des Live-Board-Scans (`_board_day_midnight_ok`).

Parität zum iOS `BoardObservationDateGate` (Sebastian LH686 2026-07-20):
eine Board-Row derselben täglichen Flugnummer zählt nur dann zum angefragten
Verkehrstag, wenn ihr sched-Datum exakt passt ODER um genau einen Tag nahe
der Tagesgrenze abweicht (Red-Eye über Mitternacht).
"""
import app as backend


def test_gleicher_tag_passt():
    assert backend._board_day_midnight_ok('2026-07-20', '2026-07-20T10:10:00+02:00')


def test_zwei_tage_daneben_verworfen():
    # Sebastians Fall: Mittwoch-Leg, Montags-Board-Row derselben LH686.
    assert not backend._board_day_midnight_ok('2026-07-22', '2026-07-20T10:10:00+02:00')


def test_folgetag_red_eye_vor_3_uhr_passt():
    assert backend._board_day_midnight_ok('2026-07-20', '2026-07-21T00:45:00+02:00')
    assert backend._board_day_midnight_ok('2026-07-20', '2026-07-21 02:59:00')


def test_folgetag_nach_3_uhr_verworfen():
    assert not backend._board_day_midnight_ok('2026-07-20', '2026-07-21T06:50:00+02:00')


def test_vortag_ab_21_uhr_passt():
    assert backend._board_day_midnight_ok('2026-07-20', '2026-07-19T23:55:00+02:00')
    assert backend._board_day_midnight_ok('2026-07-20', '2026-07-19T21:00:00')


def test_vortag_vor_21_uhr_verworfen():
    assert not backend._board_day_midnight_ok('2026-07-20', '2026-07-19T18:00:00+02:00')


def test_folgetag_ohne_uhrzeit_verworfen():
    # Nur Datum, keine Zeit → Tagesgrenzen-Nähe nicht bestimmbar → fail-closed.
    assert not backend._board_day_midnight_ok('2026-07-20', '2026-07-21')


def test_kaputte_eingaben_fail_closed():
    assert not backend._board_day_midnight_ok('heute', '2026-07-20T10:10:00')
    assert not backend._board_day_midnight_ok('2026-07-20', 'garbage')
