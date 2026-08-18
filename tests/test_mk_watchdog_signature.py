# -*- coding: utf-8 -*-
"""Watchdog-Kills buendeln zu EINER Mail-Signatur pro (Build, Art).

18.08.2026: 65 „[AeroX CRASH NEU]"-Mails fuer EINEN Befund — bei
0x8BADF00D steht der Haupt-Thread irgendwo mitten in SwiftUI, der
oberste Frame ist Zufall, jede Stack-Permutation ergab eine „neue"
Signatur. Watchdog-Kills werden jetzt pro (Build, Watchdog-Art)
dedupliziert; echte Crashes behalten die Frame-Signatur.
"""

import app

_TERM_SCENE = ('<RBSTerminateContext| domain:10 code:0x8BADF00D '
               'explanation:scene-update watchdog transgression: app<x> '
               'exhausted real (wall clock) time allowance')
_TERM_5S = ('<RBSTerminateContext| domain:10 code:0x8BADF00D '
            'explanation:[app<aerotax.AeroTax>:1] Failed to terminate '
            'gracefully after 5.0s')


def test_watchdog_ist_eine_signatur_pro_build_und_art():
    a = app._mk_signature('crash', '356', ['SwiftUICore +1', 'AttributeGraph +2'],
                          termination_reason=_TERM_5S)
    b = app._mk_signature('crash', '356', ['libswiftCore.dylib +9', 'AeroX +7'],
                          termination_reason=_TERM_5S)
    assert a == b            # andere Frames, gleiche Art → EINE Signatur
    c = app._mk_signature('crash', '356', ['SwiftUICore +1'],
                          termination_reason=_TERM_SCENE)
    assert c != a            # scene-update ist eine andere Art
    d = app._mk_signature('crash', '357', ['SwiftUICore +1'],
                          termination_reason=_TERM_5S)
    assert d != a            # anderer Build → andere Signatur


def test_watchdog_braucht_keinen_stack():
    assert app._mk_signature('crash', '356', [],
                             termination_reason=_TERM_5S) is not None


def test_echte_crashes_behalten_frame_signatur():
    a = app._mk_signature('crash', '356', ['AeroX +1', 'SwiftUICore +2'])
    b = app._mk_signature('crash', '356', ['SwiftUICore +2', 'AeroX +1'])
    assert a != b            # Frame-Reihenfolge unterscheidet weiter
    assert app._mk_signature('crash', '356', []) is None
    # Offsets aendern die Signatur nicht (nur Binaernamen zaehlen).
    c = app._mk_signature('crash', '356', ['AeroX +99', 'SwiftUICore +5'])
    assert a == c
