"""Calendar-Feed-URL-Sanitize-Tests (Root-Cause Johanna, 2026-07-15).

Johanna (neue Nutzerin) fügte ihren LH-myTime-Roster-Link ein — mit LEERZEICHEN
mitten im Pfad+Query (Paste-Artefakte):
    webcal://api.lufthansa.com/ mytime/ mytime/rostershareinfo/ downloadRoster? api_key=...
Der `.strip()` fasste nur die Enden an → die inneren Spaces rutschten durch und
der Abruf starb als generischer Fehler.

Getestet wird die reine Sanitize-Funktion `_sanitize_feed_url` (defensiv für
alte Clients, die client-seitig noch nicht sanitizen). KEIN echter api_key —
alle Keys synthetisch.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as backend


SYN_KEY = "SYNTHETIC_TEST_KEY_00000"


def test_removes_internal_spaces_from_johanna_url():
    raw = (f"webcal://api.lufthansa.com/ mytime/ mytime/rostershareinfo/ "
           f"downloadRoster? api_key={SYN_KEY}")
    cleaned = backend._sanitize_feed_url(raw)
    assert " " not in cleaned
    assert cleaned == (f"webcal://api.lufthansa.com/mytime/mytime/rostershareinfo/"
                       f"downloadRoster?api_key={SYN_KEY}")


def test_removes_newlines_and_tabs_anywhere():
    raw = f"webcal://host.example/\n path/\t downloadRoster?api_key={SYN_KEY}\n"
    cleaned = backend._sanitize_feed_url(raw)
    assert "\n" not in cleaned and "\t" not in cleaned and " " not in cleaned
    assert cleaned == f"webcal://host.example/path/downloadRoster?api_key={SYN_KEY}"


def test_removes_zero_width_and_bom_chars():
    raw = ("﻿https://host.example/​path/"
           f"downloadRoster?api_key={SYN_KEY}‎")
    cleaned = backend._sanitize_feed_url(raw)
    for bad in ("﻿", "​", "‎"):
        assert bad not in cleaned
    assert cleaned == f"https://host.example/path/downloadRoster?api_key={SYN_KEY}"


def test_already_clean_url_is_identical():
    raw = f"https://api.lufthansa.com/mytime/rostershareinfo/downloadRoster?api_key={SYN_KEY}"
    assert backend._sanitize_feed_url(raw) == raw


def test_empty_and_none_are_safe():
    assert backend._sanitize_feed_url("") == ""
    assert backend._sanitize_feed_url(None) == ""


def test_sanitized_johanna_url_would_reach_https_gate():
    # Nach Sanitize + webcal→https gäbe es eine gültige https-URL (der Abruf
    # kann erst danach beim echten Server scheitern, nicht schon am Format).
    raw = (f"webcal://api.lufthansa.com/ mytime/ rostershareinfo/ "
           f"downloadRoster? api_key={SYN_KEY}")
    cleaned = backend._sanitize_feed_url(raw)
    low = cleaned.lower()
    if low.startswith("webcal://"):
        cleaned = "https://" + cleaned[len("webcal://"):]
    assert cleaned.startswith("https://")
