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
unten) · position · text{title,subtitle} · pre_phase/pre_phase_label
(PRE-FLIGHT-TIMELINE, ADDITIV 2026-07-12).
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
  "pre_phase": null,
  "pre_phase_label": null,
  "text": {"title": "Fliegt gerade"}
}'''

# ── PRE-FLIGHT-Vertrags-JSON (Timeline 2026-07-12, identisch in iOS) ─────────
# Szenario: OUTSTATION-Wartephase um 04:45Z, iCal-Pickup 04:30Z, Board-Delay
# +10 am Abflug → Phase „Im Crewbus", Label auch als Subtitle-Suffix.
CONTRACT_PRE_JSON = '''{
  "state": "pre_flight",
  "current_leg": {
    "dep": "BCN",
    "arr": "FRA",
    "flight_no": "LH1139",
    "dep_iso": "2026-07-09T06:40:00Z",
    "arr_iso": "2026-07-09T08:55:00Z",
    "reg": "D-AIMC",
    "est_dep_iso": "2026-07-09T06:50:00Z",
    "est_arr_iso": null,
    "delay_min": 10,
    "delay_side": "dep",
    "delay_known": true,
    "status": null,
    "cancelled": null
  },
  "position": null,
  "pre_phase": "crewbus",
  "pre_phase_label": "Im Crewbus",
  "text": {"title": "Wartet auf LH1139 · 06:50", "subtitle": "BCN → FRA · Im Crewbus"}
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


def _resolve_pre():
    """OUTSTATION-Pre-Flight um 04:45Z: Pickup 04:30Z (aus dem iCal geparst),
    Board kennt +10 dep-Delay → pre_phase 'crewbus' (Pickup ≤ now < +25')."""
    return resolve_crew_live_state(
        SECTORS,
        lambda fno, frm, to: {'dep_delay_min': 10, 'reg': 'D-AIMC'},
        lambda fno, frm, to: None,
        datetime(2026, 7, 9, 4, 45, tzinfo=timezone.utc),
        homebase='FRA',
        city_lookup=lambda c: {'FRA': 'Frankfurt', 'BCN': 'Barcelona'}.get(c),
        pre_ctx={'pickup': datetime(2026, 7, 9, 4, 30, tzinfo=timezone.utc),
                 'report': None, 'commute_minutes': None})


def test_wire_contract_current_leg_exact():
    """current_leg muss EXAKT die Vertrags-Felder tragen (Namen UND Werte)."""
    want = json.loads(CONTRACT_JSON)
    got = _resolve()
    assert got['state'] == want['state']
    assert got['position'] == want['position']
    # PRE-FLIGHT-TIMELINE (ADDITIV): im flying-Zustand IMMER null.
    assert got['pre_phase'] == want['pre_phase']
    assert got['pre_phase_label'] == want['pre_phase_label']
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


def test_wire_contract_pre_flight_timeline_exact():
    """PRE-FLIGHT-Vertrag (Timeline 2026-07-12): pre_phase + Label + der
    fertige Subtitle-Text sind gepinnt — iOS zeigt sie 1:1 (statusLabel)."""
    want = json.loads(CONTRACT_PRE_JSON)
    got = _resolve_pre()
    assert got['state'] == want['state']
    assert got['position'] == want['position']
    assert got['pre_phase'] == want['pre_phase']
    assert got['pre_phase_label'] == want['pre_phase_label']
    assert got['text']['title'] == want['text']['title']
    assert got['text']['subtitle'] == want['text']['subtitle']
    leg = got['current_leg']
    for k, v in want['current_leg'].items():
        assert k in leg, f'Vertrags-Feld {k} fehlt im Server-Leg'
        assert leg[k] == v, f'{k}: server={leg[k]!r} vertrag={v!r}'


def test_wire_contract_json_is_valid_and_stable():
    """Das eingecheckte Fixture selbst bleibt parsebar & vollständig —
    schützt gegen versehentliche Edits am Fixture-Text."""
    for src in (CONTRACT_JSON, CONTRACT_PRE_JSON):
        d = json.loads(src)
        assert set(d) == {'state', 'current_leg', 'position', 'text',
                          'pre_phase', 'pre_phase_label'}
        assert set(d['current_leg']) == {
            'dep', 'arr', 'flight_no', 'dep_iso', 'arr_iso', 'reg',
            'est_dep_iso', 'est_arr_iso', 'delay_min', 'delay_side',
            'delay_known', 'status', 'cancelled'}
