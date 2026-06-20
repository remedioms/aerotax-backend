"""
Kuratierte Eckdaten gängiger Verkehrsflugzeug-Muster — OFFLINE, NULL API.

Pro ICAO-Typecode: typische Sitzplätze, Reichweite (NM), Reise-Speed (kt),
Wake-Kategorie (L/M/H/J) und Rumpf (narrow/wide). Bewusst nur die häufigsten
~70 kommerziellen Muster (deckt den Großteil des Radar-Verkehrs ab); fehlt ein
Typ, liefert die Engine einfach kein specs-Objekt (keine erfundenen Werte).

Quelle: öffentlich bekannte Hersteller-Eckdaten, gerundet. „seats" = typische
2-Klassen-Bestuhlung. cruise_kt ≈ Mach × Schallgeschwindigkeit auf Reiseflughöhe.
"""

# typecode: (seats, range_nm, cruise_kt, wtc, body)
_SPECS = {
    # ── Airbus narrowbody ──
    "A318": (115, 3100, 447, "M", "narrow"),
    "A319": (140, 3700, 447, "M", "narrow"),
    "A320": (165, 3300, 447, "M", "narrow"),
    "A321": (200, 3200, 447, "M", "narrow"),
    "A19N": (140, 4600, 450, "M", "narrow"),   # A319neo
    "A20N": (165, 3500, 450, "M", "narrow"),   # A320neo
    "A21N": (200, 4000, 450, "M", "narrow"),   # A321neo
    "BCS1": (135, 3100, 447, "M", "narrow"),   # A220-100
    "BCS3": (160, 3400, 447, "M", "narrow"),   # A220-300
    # ── Airbus widebody ──
    "A332": (250, 7250, 470, "H", "wide"),
    "A333": (290, 6350, 470, "H", "wide"),
    "A338": (260, 8150, 470, "H", "wide"),     # A330-800neo
    "A339": (300, 7200, 470, "H", "wide"),     # A330-900neo
    "A342": (260, 7800, 470, "H", "wide"),
    "A343": (290, 7400, 470, "H", "wide"),
    "A345": (320, 8000, 470, "H", "wide"),
    "A346": (370, 7900, 470, "H", "wide"),
    "A359": (315, 8100, 488, "H", "wide"),     # A350-900
    "A35K": (370, 8700, 488, "H", "wide"),     # A350-1000
    "A388": (525, 8000, 490, "J", "wide"),     # A380
    # ── Boeing 737 ──
    "B732": (130, 2300, 430, "M", "narrow"),
    "B733": (140, 2400, 430, "M", "narrow"),
    "B734": (150, 2400, 430, "M", "narrow"),
    "B735": (120, 2400, 430, "M", "narrow"),
    "B736": (110, 3200, 447, "M", "narrow"),
    "B737": (140, 3000, 447, "M", "narrow"),   # 737-700
    "B738": (175, 3000, 447, "M", "narrow"),   # 737-800
    "B739": (190, 2900, 447, "M", "narrow"),   # 737-900
    "B38M": (175, 3550, 447, "M", "narrow"),   # 737 MAX 8
    "B39M": (195, 3300, 447, "M", "narrow"),   # 737 MAX 9
    "B37M": (150, 3850, 447, "M", "narrow"),   # 737 MAX 7
    # ── Boeing 757/767 ──
    "B752": (200, 3900, 458, "M", "narrow"),
    "B753": (240, 3400, 458, "M", "narrow"),
    "B762": (215, 6600, 470, "H", "wide"),
    "B763": (260, 5980, 470, "H", "wide"),
    "B764": (245, 5625, 470, "H", "wide"),
    # ── Boeing 747 ──
    "B742": (370, 6900, 490, "H", "wide"),
    "B744": (410, 7670, 490, "H", "wide"),
    "B748": (410, 7730, 490, "J", "wide"),     # 747-8
    # ── Boeing 777 ──
    "B772": (305, 9700, 482, "H", "wide"),
    "B77L": (300, 9395, 482, "H", "wide"),     # 777-200LR
    "B773": (370, 7370, 482, "H", "wide"),
    "B77W": (365, 7370, 482, "H", "wide"),     # 777-300ER
    # ── Boeing 787 ──
    "B788": (240, 7355, 488, "H", "wide"),
    "B789": (290, 7635, 488, "H", "wide"),
    "B78X": (330, 6430, 488, "H", "wide"),     # 787-10
    # ── Embraer ──
    "E170": (76, 2150, 430, "M", "narrow"),
    "E75L": (88, 2200, 430, "M", "narrow"),    # E175
    "E75S": (88, 2200, 430, "M", "narrow"),
    "E190": (100, 2450, 447, "M", "narrow"),
    "E195": (120, 2300, 447, "M", "narrow"),
    "E290": (100, 2850, 447, "M", "narrow"),   # E190-E2
    "E295": (130, 2600, 447, "M", "narrow"),   # E195-E2
    # ── Regional / Turboprop ──
    "CRJ2": (50, 1700, 424, "M", "narrow"),
    "CRJ7": (70, 1700, 447, "M", "narrow"),
    "CRJ9": (90, 1550, 447, "M", "narrow"),
    "CRJX": (100, 1600, 447, "M", "narrow"),   # CRJ-1000
    "DH8D": (78, 1100, 360, "M", "narrow"),    # Dash 8 Q400
    "AT72": (70, 825, 275, "M", "narrow"),     # ATR 72
    "AT76": (70, 900, 275, "M", "narrow"),     # ATR 72-600
    "AT45": (48, 700, 265, "M", "narrow"),     # ATR 42
    "SF34": (34, 900, 250, "L", "narrow"),     # Saab 340
    # ── Business / Embraer regional jets ──
    "GLF5": (16, 6750, 488, "M", "narrow"),    # Gulfstream G550
    "GLF6": (18, 7000, 488, "M", "narrow"),    # G650
    "C25A": (8, 1900, 400, "L", "narrow"),     # Citation CJ2
    "E55P": (8, 1800, 405, "L", "narrow"),     # Phenom 300
}

# Wake-Kategorie → Klartext.
_WTC = {"L": "Leicht", "M": "Mittel", "H": "Schwer", "J": "Super (A380)"}


def specs_for_type(typecode):
    """ICAO-Typecode → dict mit seats/range_nm/cruise_kt/wake/body, oder None."""
    if not typecode:
        return None
    t = _SPECS.get(str(typecode).strip().upper())
    if not t:
        return None
    seats, rng, cruise, wtc, body = t
    return {
        "seats": seats,
        "range_nm": rng,
        "cruise_kt": cruise,
        "wake": wtc,
        "wake_label": _WTC.get(wtc, wtc),
        "body": body,
    }
