"""Pattern A: Conservative Z73 when target_iata unknown (2026-05-24).

Tibor-FollowMe-Diff zeigt 3 Tage wo AeroTAX Z76 mit Default-Pauschale 28€
ausgibt obwohl FollowMe Z73 Inland 14€ rechnet — weil das Ziel-Land aus
dem Cluster nicht ermittelbar war.

Betroffene Tage 2025:
- 2025-03-29 Tour 13 Anreise → Mumbai (Cluster konnte BOM nicht extrahieren)
- 2025-04-08 Tour 14 Anreise → Seoul (Cluster konnte ICN nicht extrahieren)
- 2025-10-05 Tour 41 Anreise → Seoul (Cluster konnte ICN nicht extrahieren)

Fix: Wenn target_iata leer ist UND der Tag An/Ab-Tag ist → Z73 statt Z76.
Volltage (Mid-Tour) ohne Ziel bleiben Z76 (Übernachtung faktisch im Ausland).

Diese Tests prüfen die Code-Logik direkt — keine PII-Daten nötig.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app  # noqa: E402


def test_classify_call_v8_with_unknown_target_anreise_yields_z73():
    """Code-Pattern: target_iata leer + is_anreise → Z73 (statt Z76 28€)."""
    src = open(app.__file__, encoding='utf-8').read()

    # Pattern: in der "Homebase-Stempel auf Auslandstour"-Logik muss es einen
    # if-not-target_iata-Branch geben, der Z73 setzt.
    assert "if not target_iata and (is_anreise or is_abreise):" in src, (
        "Pattern A Fix fehlt: conservative-Z73-Branch nicht gefunden im "
        "Homebase-Stempel-Codepfad. Siehe FollowMe-Diff 2025-03-29/04-08/10-05."
    )

    # Der Branch muss Z73 setzen und INLAND_AN_ABREISE als Betrag verwenden
    # (in der Nähe der Conditional).
    idx = src.find("if not target_iata and (is_anreise or is_abreise):")
    snippet = src[idx:idx + 800]
    assert "klass = 'Z73'" in snippet, "Pattern A Branch setzt nicht Z73"
    assert "INLAND_AN_ABREISE" in snippet, "Pattern A Branch nutzt nicht INLAND_AN_ABREISE"
    assert "v14-conservative-z73" in snippet, "Pattern A Audit-Log fehlt"


def test_volltag_without_target_stays_z76():
    """Code-Pattern: Volltag ohne Ziel bleibt Z76 (Übernachtung faktisch Ausland)."""
    src = open(app.__file__, encoding='utf-8').read()
    # Nach dem conservative-Z73 muss der else-Branch (Z76) intakt sein
    idx = src.find("if not target_iata and (is_anreise or is_abreise):")
    snippet = src[idx:idx + 1500]
    # Volltag bleibt Z76 mit Default 28€
    assert "voll_24h" in snippet, (
        "Pattern A: Volltag-Z76-Pfad muss erhalten bleiben für Mid-Tour ohne Ziel"
    )
    assert "'Z76'" in snippet, "Pattern A: Z76-else-Branch fehlt"


def test_bmf_inland_an_abreise_value_unchanged():
    """Sanity: BMF Inland An/Abreise bleibt 14.00€ in BMF_INLAND_BY_YEAR[2025]."""
    inland_2025 = app.BMF_INLAND_BY_YEAR.get(2025)
    assert inland_2025 is not None, "BMF_INLAND_BY_YEAR[2025] fehlt"
    assert float(inland_2025.get('an_abreise')) == 14.0, (
        f"BMF Inland An/Abreise 2025 hat sich geändert: {inland_2025.get('an_abreise')}"
    )
