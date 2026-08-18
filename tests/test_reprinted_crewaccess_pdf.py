# -*- coding: utf-8 -*-
"""Gedruckte CrewAccess-PDFs (LHX): CID-Reparatur muss das Ergebnis annehmen.

Fall Christoph S. (LHX/MUC, 18.08.2026, ax_logbook_upload id 606,
``unsupported_pdf_format``): iOS „Drucken → Als PDF sichern" erzeugt eine
kaputte ToUnicode-CMap, pdfplumber liefert nur ``(cid:NN)``-Tokens. Die
CID+29-Reparatur dekodierte den Text perfekt, wurde aber verworfen, weil nur
die drei DISCOVER-Marker als Beweis galten — CrewAccess-„Roster Preview"
fiel durch und JEDER nachgelagerte Parser sah CID-Müll.

Die Fixtures sind SYNTHETISCH (aus dem öffentlich bekannten Kopfformat
gebaut) — keine echten Nutzerdaten im Repo.
"""

import app


def _als_cid(text):
    """Kodiert Klartext in die (cid:NN)-Form des defekten Exports
    (Codepoint − 29), Zeilenumbrüche bleiben roh — wie im echten Extrakt."""
    out = []
    for ch in text:
        if ch == "\n":
            out.append("\n")
        else:
            out.append("(cid:%d)" % (ord(ch) - 29))
    return "".join(out)


CREWACCESS_KOPF = (
    "Roster Preview\n"
    "Planning period: September 2026\n"
    "CSN, Mustermann Chris\n"
    "Rank: JC   Base: MUC\n"
    "Date Report (UTC) Tags Pos Activity From To Start (UTC) End (UTC) A/C Layover Trip ID\n"
    "07 Mon 12:05 FAM_C EC 2220 MUC TLS 13:25 15:15 32N 01886\n"
)


def test_gedruckte_crewaccess_pdf_wird_repariert():
    raw = _als_cid(CREWACCESS_KOPF)
    repaired = app._repair_ios_reprinted_roster_text(raw)
    assert "Roster Preview" in repaired
    assert "Planning period: September 2026" in repaired
    assert "(cid:" not in repaired
    # Und der CrewAccess-Torwaechter wuerde jetzt greifen (die Zeile, an der
    # der 422 vorher entstand).
    assert "Roster Preview" in repaired[:400]


def test_fremder_cid_muell_bleibt_unangetastet():
    # Viele CID-Tokens, aber KEINE bekannten Strukturmarker nach Dekodierung:
    # die Reparatur darf das Ergebnis nicht uebernehmen (Schutz gegen
    # Fehl-Dekodierung fremder PDFs).
    raw = "".join("(cid:%d)" % (40 + (i % 50)) for i in range(200))
    assert app._repair_ios_reprinted_roster_text(raw) == raw


def test_discover_pfad_unveraendert():
    kopf = (
        "Roster\n"
        "Period: September 2026\n"
        + app._DISCOVER_HEADER + "\n"
    )
    repaired = app._repair_ios_reprinted_roster_text(_als_cid(kopf))
    assert "Period: September 2026" in repaired
    assert "(cid:" not in repaired
