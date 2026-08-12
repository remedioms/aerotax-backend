"""Merge-Semantik des Flugbuch-Wächters (tools/logbook-parsers).

Der Wächter verschmilzt neue Parser-Legs mit dem BESTEHENDEN
`ax_logbook_import` des Nutzers (eine Zeile pro Token). Diese Tests sichern
die drei Eigenschaften, an denen Datenverlust oder Erfindung hinge:

  1. Union statt Ersetzen — Bestehendes überlebt jeden Re-Import.
  2. Identischer Schlüssel + identische Blockzeit = Dublette (kein Doppel-Leg).
  3. Identischer Schlüssel + ABWEICHENDE Blockzeit = Konflikt → ValueError
     (der Wächter schickt den Batch dann in die manuelle Prüfung).
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "tools", "logbook-parsers"))

from logbook_watchdog import merge_legs, merge_sims  # noqa: E402


def _leg(date, flight, frm, to, dep, block, **kw):
    leg = {"date": date, "flight": flight, "from": frm, "to": to,
           "dep_iso": dep, "block_min": block}
    leg.update(kw)
    return leg


OLD = [
    _leg("2022-07-01", "LH100", "FRA", "JFK", "2022-07-01T10:00:00Z", 480),
    _leg("2022-07-03", "LH101", "JFK", "FRA", "2022-07-03T22:00:00Z", 430),
]


def test_union_keeps_existing_and_adds_new():
    new = [_leg("2026-02-06", "LH1642", "MUC", "GDN",
                "2026-02-06T10:26:00Z", 92)]
    merged, added = merge_legs(OLD, new)
    assert added == 1
    assert len(merged) == 3
    # Bestehende Legs unverändert enthalten (Identität, nicht nur Anzahl).
    for leg in OLD:
        assert leg in merged


def test_duplicate_key_same_block_is_noop():
    merged, added = merge_legs(OLD, [dict(OLD[0])])
    assert added == 0
    assert len(merged) == 2


def test_duplicate_key_different_block_raises():
    clash = dict(OLD[0], block_min=999)
    with pytest.raises(ValueError, match="Merge-Konflikt"):
        merge_legs(OLD, [clash])


def test_merge_sorts_by_date_then_dep():
    new = [_leg("2022-07-01", "LH050", "FRA", "MUC",
                "2022-07-01T06:00:00Z", 60)]
    merged, _ = merge_legs(OLD, new)
    assert [l["flight"] for l in merged] == ["LH050", "LH100", "LH101"]


def test_legs_without_dep_iso_fall_back_to_block_key():
    # Alt-Importe (z.B. Condor-Historie) können Legs ohne dep_iso tragen —
    # der Schlüssel weicht dann auf die Blockzeit aus, statt zu kollidieren.
    a = {"date": "2024-01-01", "flight": "DE123", "from": "FRA", "to": "PMI",
         "block_min": 130}
    b = dict(a, block_min=131)  # gleiche Strecke, anderer Block ⇒ eigener Key
    merged, added = merge_legs([a], [b])
    assert added == 1 and len(merged) == 2


def test_empty_existing_is_fine():
    merged, added = merge_legs(None, OLD)
    assert added == 2 and len(merged) == 2


def test_merge_sims_dedupes_and_sorts():
    old = [{"date": "2022-05-02", "code": "MUC327", "duration_min": 240}]
    new = [dict(old[0]),
           {"date": "2022-05-01", "code": "MUC327", "duration_min": 240}]
    merged = merge_sims(old, new)
    assert len(merged) == 2
    assert merged[0]["date"] == "2022-05-01"
