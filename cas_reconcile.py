"""cas_reconcile — Abgleich deterministische CAS-Fakten <-> Sonnet-Reader.

Teil 2 von "beides kombiniert":
    Der deterministische Parser (cas_table_parser) liefert die HARTEN Fakten
    (Datum, Flugnummern, Routing, UTC-Zeiten). Der Sonnet-Reader liefert die
    weiche Interpretation (activity_type, overnight_after_day, layover_iata ...).

    Diese Funktion verheiratet beide:
      - Wo der Parser sicher ist (Flug vorhanden, Routing, Zeiten) → diese Fakten
        gelten und ueberschreiben abweichende LLM-Werte (deterministisch gewinnt).
      - Nachtflug-Heimkehr (overnight) wird ZEITBASIERT aus den UTC-Zeiten +
        Flughafen-Zeitzone berechnet (tz_midnight), nicht geraten.
      - Jede Abweichung LLM vs deterministisch wird als Korrektur protokolliert,
        sodass nichts still passiert.
    Ergebnis: gleiche Eingabe -> gleiche harten Felder, unabhaengig von LLM-Laune.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

try:
    import tz_midnight as _tzm
except Exception:  # pragma: no cover
    _tzm = None  # type: ignore

try:
    from airport_tz import airport_country as _atz_country
except Exception:  # pragma: no cover
    _atz_country = None  # type: ignore


def _is_foreign(iata: str, homebase: str) -> Optional[bool]:
    if not (_atz_country and iata):
        return None
    iso = _atz_country(iata.upper())
    if not iso:
        return None
    return iso != 'DE'


def reconcile_day(det: Dict[str, Any], llm: Dict[str, Any], homebase: str) -> Dict[str, Any]:
    """Vereint einen deterministischen Tag (det) mit dem LLM-Tag (llm).
    Returnt den korrigierten LLM-Tag + 'reconcile' Audit-Liste.
    Mutiert llm nicht; gibt eine Kopie zurueck.
    """
    out = dict(llm)
    corrections: List[str] = []

    # 1) Flugnummern: deterministisch gewinnt, wenn vorhanden
    det_fl = det.get('flight_numbers') or []
    if det_fl:
        llm_fl = out.get('flight_numbers') or []
        if set(det_fl) != set(llm_fl):
            corrections.append(f'flight_numbers {llm_fl} -> {det_fl} (deterministisch)')
            out['flight_numbers'] = det_fl
        if not out.get('has_flight_segment'):
            out['has_flight_segment'] = True
            corrections.append('has_flight_segment false -> true (Flug im PDF erkannt)')

    # 2) Routing-IATAs: deterministisch ergaenzen (nie loeschen)
    det_route = [r for r in (det.get('routing') or []) if r != homebase.upper()]
    if det_route:
        llm_route = out.get('routing_iatas') or []
        merged = list(dict.fromkeys([*llm_route, *det['routing']]))
        if merged != llm_route:
            corrections.append(f'routing_iatas {llm_route} -> {merged} (deterministisch ergaenzt)')
            out['routing_iatas'] = merged

    # 2b) Reiner Layover-Tag: PDF zeigt einen Auslands-Ort OHNE eigenen Flug
    #     (z. B. "X BLR" zwischen Hin- und Rueckflug). Das ist deterministisch eine
    #     Auslands-Uebernachtung — auch wenn der LLM den Tag als frei verkannt hat
    #     (ZeroDay). Wir setzen layover_iata + overnight, damit die Hotelnacht-/
    #     VMA-Zaehlung stabil ist (Konvergenz der Hotelnaechte).
    if not (det.get('flight_numbers')):
        foreign = [r for r in (det.get('routing') or [])
                   if r and r.upper() != homebase.upper() and _is_foreign(r, homebase)]
        if foreign:
            ap = foreign[-1].upper()
            if (out.get('layover_iata') or '').upper() != ap:
                corrections.append(f"layover_iata {out.get('layover_iata')} -> {ap} (Layover-Tag aus PDF)")
                out['layover_iata'] = ap
            if not out.get('overnight_after_day'):
                corrections.append('overnight_after_day False -> True (Auslands-Layover-Tag aus PDF)')
                out['overnight_after_day'] = True
            out['tz_hotel_night'] = True
            out.setdefault('activity_type', 'tour_continuation')

    # 3) Zwei getrennte, zeitbasierte Flags (VMA vs Hotelnacht) — audit-sicher.
    vh = compute_vma_and_hotel(det, homebase)
    if vh:
        if vh.get('overnight_vma') is not None:
            out['tz_overnight_vma'] = vh['overnight_vma']
        if vh.get('hotel_night') is not None:
            out['tz_hotel_night'] = vh['hotel_night']
        out['tz_flags_reason'] = vh.get('reason')
        # overnight_after_day (Legacy-Feld, das normalized_tours liest) auf den
        # VMA-Begriff setzen — das ist die steuerlich relevante "auswaerts um
        # 24:00"-Aussage. Nur korrigieren wenn klar abweichend.
        vma = vh.get('overnight_vma')
        if vma is not None:
            llm_ov = bool(out.get('overnight_after_day'))
            if vma != llm_ov:
                corrections.append(
                    f'overnight_after_day {llm_ov} -> {vma} (zeitbasiert VMA: {vh.get("reason")})'
                )
                out['overnight_after_day'] = vma

    if corrections:
        out['reconcile'] = corrections
        out['reconcile_source'] = 'cas_table_parser+tz_midnight'
    return out


def _compute_overnight(det: Dict[str, Any], homebase: str) -> Optional[bool]:
    """Zeitbasierte Uebernachtungs-Erkennung fuer einen Flug-Tag.
    True = der Flug landet (lokal) erst am Folgetag des lokalen Abflugdatums,
    der Crew-Member ist also ueber lokale Mitternacht des Abflugorts weg.
    None wenn nicht sicher ableitbar.
    """
    if _tzm is None:
        return None
    datum = det.get('datum')
    dep_t = det.get('dep_time')
    arr_t = det.get('arr_time')
    route = det.get('routing') or []
    if not (datum and dep_t and arr_t and len(route) >= 2 and dep_t != arr_t):
        return None
    dep_iata, arr_iata = route[0], route[-1]
    try:
        return _tzm.is_night_return_flight(datum, dep_t, dep_iata, arr_t, arr_iata)
    except Exception:  # pragma: no cover
        return None


def compute_vma_and_hotel(det: Dict[str, Any], homebase: str) -> Dict[str, Any]:
    """Trennt die zwei steuerlich VERSCHIEDENEN Tatsachen eines Flug-Tags,
    rein zeitbasiert aus den deterministischen CAS-Fakten. Audit-sicher: jede
    Aussage kommt mit Begruendung; bei Unsicherheit None statt Annahme.

    Zwei getrennte Flags (siehe BMF §9 EStG):
      overnight_vma  — Verpflegungsmehraufwand-relevant: ist die Person um 24:00
                       Ortszeit NICHT zuhause? (am Layover ODER im Flug ueber
                       Mitternacht). Auch der Nachtflug-Heimkehrtag zaehlt hier.
      hotel_night    — echte Hotel-Uebernachtung am Zielort: heute auswaerts
                       GELANDET und dort geblieben (nicht im Flugzeug, nicht heim).

    Regel:
      arrival_home   = Ziel == Homebase
      crosses_mid    = Flug ueber lokale Mitternacht des Abflugorts
      overnight_vma  = (nicht arrival_home) ODER crosses_mid
      hotel_night    = (nicht arrival_home) UND (nicht crosses_mid)

    Returnt {} wenn kein auswertbarer Flug-Tag.
    """
    datum = det.get('datum')
    dep_t = det.get('dep_time')
    arr_t = det.get('arr_time')
    route = det.get('routing') or []
    if not (det.get('flight_numbers') and datum and dep_t and len(route) >= 2):
        return {}

    hb = (homebase or 'FRA').upper()
    dep_iata, arr_iata = route[0].upper(), route[-1].upper()
    arrival_home = (arr_iata == hb)

    crosses_mid = None
    if arr_t and dep_t != arr_t and _tzm is not None:
        try:
            crosses_mid = _tzm.is_night_return_flight(datum, dep_t, dep_iata, arr_t, arr_iata)
        except Exception:  # pragma: no cover
            crosses_mid = None
    # Ohne Ankunftszeit koennen wir crosses_mid nicht sicher bestimmen.
    if crosses_mid is None:
        # Konservativ: ein Heimflug ohne Ankunftszeit -> kein VMA/Hotel-Override
        # (LLM behaelt das Sagen). Ein Auswaerts-Flug ohne Ankunftszeit -> away,
        # aber Hotel/Transit unklar -> nur overnight_vma sicher.
        if not arrival_home:
            return {
                'overnight_vma': True,
                'hotel_night': None,
                'reason': (f'overnight_vma=True: Ziel {arr_iata} != Homebase {hb} '
                           f'(auswaerts). hotel_night unbestimmt (keine Ankunftszeit).'),
            }
        return {}

    overnight_vma = (not arrival_home) or bool(crosses_mid)
    hotel_night = (not arrival_home) and (not crosses_mid)

    reason = (
        f'arr={arr_iata} home={arrival_home} crosses_midnight={crosses_mid} '
        f'(dep {dep_iata} {dep_t} -> arr {arr_iata} {arr_t}) => '
        f'overnight_vma={overnight_vma}, hotel_night={hotel_night}'
    )
    return {'overnight_vma': overnight_vma, 'hotel_night': hotel_night, 'reason': reason}


def reconcile_days(det_days: List[Dict[str, Any]], llm_days: List[Dict[str, Any]],
                   homebase: str) -> Dict[str, Any]:
    """Gleicht ganze Listen ab (gematcht ueber 'datum'). Returnt:
      {'days': [...korrigierte llm_days...], 'corrections_count': int,
       'corrections_by_date': {datum: [..]}, 'det_only_dates': [...]}
    """
    det_by_date = {d['datum']: d for d in det_days if d.get('datum')}
    out_days: List[Dict[str, Any]] = []
    by_date: Dict[str, List[str]] = {}
    total = 0
    seen_dates = set()

    for ld in llm_days:
        ds = ld.get('datum')
        seen_dates.add(ds)
        det = det_by_date.get(ds)
        if det:
            merged = reconcile_day(det, ld, homebase)
            if merged.get('reconcile'):
                by_date[ds] = merged['reconcile']
                total += len(merged['reconcile'])
            out_days.append(merged)
        else:
            out_days.append(ld)

    # Tage, die der deterministische Parser kennt, der LLM aber nicht ausgegeben hat
    det_only = [ds for ds in det_by_date if ds not in seen_dates]
    return {
        'days': out_days,
        'corrections_count': total,
        'corrections_by_date': by_date,
        'det_only_dates': det_only,
    }
