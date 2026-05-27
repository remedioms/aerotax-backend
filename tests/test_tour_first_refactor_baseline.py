"""Tour-First-Refactor Baseline-Test (R38, 2026-05-27).

Sichert Tibors aktuellen Live-Wert-Stand als „darf-nicht-schlechter-werden"
Anker. Jeder Refactor-Schritt im Tour-First-Plan muss diesen Test prüfen.

Der Test prüft NUR die Fixture (statisch), nicht die Live-Pipeline. Echt-
Validierung gegen die Backend-Berechnung folgt schrittweise wenn die
Architektur stabil-genug ist für lokale End-to-End-Tests.

Was darf passieren:
- Werte werden BESSER (z.B. Z76 steigt weil Heimkehr-Tage erkannt werden)
- Topf-Verschiebungen (z.B. -X Z73 / +Y Z76 / Brutto stabil)

Was darf NICHT passieren:
- Z77/Z17/Z73-exact-Matches gehen verloren
- Brutto-Werbungskosten fallen relevant (>50€)
- Tour-Topf-Logik wird inkonsistent (z.B. hotel_naechte > arbeitstage)
"""
import json
import os
from pathlib import Path

import pytest

_FIXTURE = (Path(__file__).parent / 'fixtures'
            / 'tibor_baseline_2026_05_27.json')


def _baseline():
    with open(_FIXTURE) as f:
        return json.load(f)


def test_baseline_fixture_exists_and_loads():
    """Baseline-Datei ist da und parsebar."""
    b = _baseline()
    assert b['_meta']['subject'].startswith('Tibor Quaas 2025')
    assert 'totals' in b
    assert 'counters' in b
    assert 'must_not_regress' in b


def test_baseline_totals_self_consistent():
    """Topf-Summe = Brutto. Brutto - Z77 - Z17 = Netto. Mathematische
    Selbst-Konsistenz der Baseline (Sicherheits-Check für die Fixture)."""
    t = _baseline()['totals']
    summe = (t['fahr'] + t['reinig'] + t['trink']
             + t['vma_72'] + t['vma_73'] + t['vma_74'] + t['vma_aus'])
    assert abs(summe - t['gesamt']) < 0.5, (
        f'Brutto-Summe {summe} ≠ gesamt {t["gesamt"]}'
    )
    netto_calc = t['gesamt'] - t['z77'] - t['ag_z17']
    assert abs(netto_calc - t['netto']) < 0.5, (
        f'Brutto-Z77-Z17 ({netto_calc}) ≠ netto ({t["netto"]})'
    )


def test_baseline_counters_plausible():
    """Hard-Plausi-Checks die jede Klassifikation erfüllen MUSS."""
    c = _baseline()['counters']
    assert c['hotel_naechte'] <= c['arbeitstage'], (
        'Hotel-Nächte > Arbeitstage ist logisch unmöglich'
    )
    assert c['fahr_tage'] <= c['arbeitstage'], (
        'Fahr-Tage > Arbeitstage ist logisch unmöglich'
    )
    assert c['arbeitstage'] < 250, (
        f'arbeitstage={c["arbeitstage"]} ist unrealistisch hoch (>250)'
    )


def test_baseline_known_edge_cases_documented():
    """Refactor muss diese 4 Tage explizit verbessern oder gleich lassen.
    Doc-Test: stellt sicher dass die known_edge_cases-Sektion existiert
    und alle Schlüsseldaten enthält."""
    edge = _baseline()['known_edge_cases']
    for datum in ('2025-02-14', '2025-03-25', '2025-03-26', '2025-12-10'):
        assert datum in edge, f'edge case {datum} fehlt'
        for key in ('user_says', 'current_klass', 'expected_klass'):
            assert key in edge[datum], f'{datum} fehlt key {key}'


def test_baseline_must_not_regress_keys_set():
    """Die Hard-Anker dürfen sich nicht ändern."""
    mn = _baseline()['must_not_regress']
    assert mn['z73_exact_match_followme'] == 140.00
    assert mn['z77_exact_match'] == 4456.00
    assert mn['z17_exact_match'] == 360.00


def test_baseline_protected_against_silent_overwrite():
    """Sicherheit: die Baseline darf nicht versehentlich überschrieben werden.
    Wenn jemand die Werte ändert ohne Refactor-Begründung → Test rot."""
    b = _baseline()
    # Snapshot-Anker — falls jemand diese Zahlen ändert, hier nachpflegen
    assert b['totals']['netto'] == 1257.12, (
        'Baseline-netto wurde geändert. Wenn Refactor das ist: Fixture '
        'aktualisieren UND CHANGELOG-Eintrag dokumentieren.'
    )
    assert b['totals']['vma_aus'] == 4865.00
    assert b['counters']['fahr_tage'] == 42
    assert b['counters']['hotel_naechte'] == 70
