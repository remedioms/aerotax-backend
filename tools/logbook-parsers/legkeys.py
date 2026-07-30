"""Leg-Key-Kollisionen auflösen — gemeinsame Nachbearbeitung aller Parser.

Das Backend schlüsselt Legs als `date|flight|from|to` (`_logbook_leg_key`)
und legt sie in ein Dict (`imp_by_key`). Zwei Legs mit identischem Schlüssel
überschreiben sich dort STILL — ein Leg samt Landungen verschwindet.

Betroffen sind vor allem FCL.050-PDF-Importe: die haben gar keine
Flugnummer, also kollidiert jede am selben Tag wiederholte Strecke
(Shuttle-Muster LEJ→BER→LEJ→BER).

Gegenmittel wie bei Christos' Import: die zweite und jede weitere Belegung
bekommt das Suffix „(2)", „(3)" … in der Flugnummer. Nichts wird gelöscht.
"""


def dedupe_keys(legs):
    """Kollidierende Leg-Keys durchnummerieren. Gibt die Liste der
    vergebenen Suffixe zurück (für den Report)."""
    seen, fixed = {}, []
    for leg in legs:
        base = (leg.get('flight') or '').upper()
        key = (leg.get('date'), base, leg.get('from'), leg.get('to'))
        n = seen.get(key, 0) + 1
        seen[key] = n
        if n > 1:
            leg['flight'] = f'{base}({n})'
            fixed.append((leg.get('date'), base or '—',
                          f"{leg.get('from')}→{leg.get('to')}",
                          (leg.get('dep_iso') or '')[11:16], n))
    return fixed
