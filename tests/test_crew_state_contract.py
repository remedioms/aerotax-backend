"""CONTRACT-TEST crew_state ↔ iOS (Crew-Feed-Härtung #3, Owner 2026-07-12).

Warum es diesen Test gibt: Die Bordkarten-Sektion war tagelang STILL tot,
weil der Server {dep, arr, flight_no, dep_iso, …} sendete und iOS das
flights_live-Format (flight/dep_iata/…) las — beide Seiten grün, Feature
kaputt. Dieser Test pinnt den WIRE-VERTRAG: das JSON unten ist BYTE-GLEICH
als Fixture im iOS-Test hinterlegt (AeroTaxTests/CrewStateContractTests.swift,
decodiert es mit APIClient.CrewState). Ändert eine Seite den Vertrag, bricht
ihr Test — nicht mehr das Feature.

Regel: Wer hier etwas ändert, MUSS die iOS-Fixture identisch mitziehen
(und umgekehrt). Keys, die iOS liest: state · current_leg (alle Felder
unten) · position · text{title,subtitle}.
"""
import json
import os
from datetime import datetime, timezone

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

from blueprints.crew_live_state import resolve_crew_live_state

# ── Das kanonische Vertrags-JSON (identisch in iOS eingecheckt) ──────────────
CONTRACT_JSON = '''{
  "state": "flying",
  "current_leg": {
    "dep": "BCN",
    "arr": "FRA",
    "flight_no": "LH1139",
    "dep_iso": "2026-07-09T06:40:00Z",
    "arr_iso": "2026-07-09T08:55:00Z",
    "reg": "D-AIMC",
    "est_dep_iso": "2026-07-09T06:50:00Z",
    "est_arr_iso": "2026-07-09T09:01:00Z",
    "delay_min": 6,
    "delay_side": "arr",
    "delay_known": true,
    "status": "airborne",
    "cancelled": null
  },
  "position": null,
  "text": {"title": "Fliegt gerade"}
}'''

SECTORS = [
    {'flight': 'LH1139', 'from': 'BCN', 'to': 'FRA',
     'dep_iso': '2026-07-09T06:40:00Z', 'arr_iso': '2026-07-09T08:55:00Z'},
]

OBS = {'LH1139': {'status': 'airborne', 'dep_delay_min': 10,
                  'arr_delay_min': 6, 'reg': 'D-AIMC'}}


def _resolve():
    return resolve_crew_live_state(
        SECTORS,
        lambda fno, frm, to: OBS.get(fno),
        lambda fno, frm, to: None,
        datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc),
        homebase='FRA',
        city_lookup=lambda c: {'FRA': 'Frankfurt', 'BCN': 'Barcelona'}.get(c))


def test_wire_contract_current_leg_exact():
    """current_leg muss EXAKT die Vertrags-Felder tragen (Namen UND Werte)."""
    want = json.loads(CONTRACT_JSON)
    got = _resolve()
    assert got['state'] == want['state']
    assert got['position'] == want['position']
    # Titel gepinnt (iOS zeigt ihn 1:1); Subtitle ist Anzeige-Prosa → nur Typ.
    assert got['text']['title'] == want['text']['title']
    assert got['text'].get('subtitle') is None \
        or isinstance(got['text']['subtitle'], str)
    # Der Kern: JEDES Vertrags-Feld exakt — und keine Vertrags-Felder, die
    # der Server plötzlich weglässt.
    leg = got['current_leg']
    for k, v in want['current_leg'].items():
        assert k in leg, f'Vertrags-Feld {k} fehlt im Server-Leg'
        assert leg[k] == v, f'{k}: server={leg[k]!r} vertrag={v!r}'


def test_wire_contract_json_is_valid_and_stable():
    """Das eingecheckte Fixture selbst bleibt parsebar & vollständig —
    schützt gegen versehentliche Edits am Fixture-Text."""
    d = json.loads(CONTRACT_JSON)
    assert set(d) == {'state', 'current_leg', 'position', 'text'}
    assert set(d['current_leg']) == {
        'dep', 'arr', 'flight_no', 'dep_iso', 'arr_iso', 'reg',
        'est_dep_iso', 'est_arr_iso', 'delay_min', 'delay_side',
        'delay_known', 'status', 'cancelled'}
