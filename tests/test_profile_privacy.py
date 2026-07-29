"""Datenschutz-Wache für das ÖFFENTLICHE Profil.

Owner-Meldung 2026-07-29: „warum sehe ich die adresse von jmd auf dem crew
profil? datenschutz!!" — angezeigt wurde der WOHNORT (die Stadt, z. B. „Wien"),
nicht die Anschrift. Das Problem war nicht die Datenmenge, sondern die
EINWILLIGUNG: `hometown` wird nicht selbst eingetragen, sondern aus der
Heimadresse abgeleitet, die der Nutzer für SMART PICKUP angibt — dort steht
ausdrücklich „verschlüsselt gespeichert".

Diese Tests halten fest, was ein FREMDER über einen Nutzer erfahren darf.
Wer hier ein Feld ergänzt, muss sich fragen: Hat der Nutzer dem ZUGESTIMMT?
"""
from unittest.mock import patch

import app as A


_PROFIL = {
    'token': 'AT-TESTCREW',
    'profile': {
        'name': 'Daniel Wildberger',
        'homebase': 'VIE',
        'position': 'FO',
        'airline': 'Austrian',
        # Aus der Pickup-Adresse abgeleitet — darf NICHT nach draußen.
        'hometown': 'Wien',
        # Die Anschrift selbst und ihre Koordinaten schon gar nicht.
        'home_address': 'Teststraße 1, 1010 Wien',
        'home_latitude': 48.2082,
        'home_longitude': 16.3738,
        'home_transport_mode': 'car',
        'avatar_url': '/api/user/avatar/x/y.jpg',
    },
}


def _public():
    with patch.object(A, '_profile_load', return_value=_PROFIL):
        return A._public_profile_projection('AT-TESTCREW')['profile']


def test_wohnort_verlaesst_das_geraet_nicht():
    """Der abgeleitete Wohnort ist KEINE öffentliche Angabe."""
    assert 'hometown' not in _public()


def test_anschrift_und_koordinaten_bleiben_privat():
    """Die Regel von 2026-06-07 gilt unverändert weiter."""
    out = _public()
    for k in ('home_address', 'home_latitude', 'home_longitude',
              'home_transport_mode'):
        assert k not in out, f'{k} darf NIEMALS im öffentlichen Profil stehen'


def test_die_erlaubten_felder_bleiben_erhalten():
    """Gegenprobe: die Crew-Discovery funktioniert weiterhin."""
    out = _public()
    assert out['name'] == 'Daniel Wildberger'
    assert out['homebase'] == 'VIE'
    assert out['position'] == 'FO'
    assert out['airline'] == 'Austrian'
    assert out['avatar_url']


def test_whitelist_ist_abschliessend():
    """Kein Feld darf sich unbemerkt hineinschleichen: die Projektion gibt
    ausschliesslich Schluessel aus der Whitelist zurueck."""
    assert set(_public()).issubset(set(A._PUBLIC_PROFILE_FIELDS) | {'role'})
