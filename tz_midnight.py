"""tz_midnight — deterministische "Ort um 24:00 Ortszeit"-Logik (BMF §9 EStG).

PROBLEM, das dieses Modul löst:
    Der bisherige Calc kennt KEINE Zeitzonen. Er verlässt sich darauf, dass der
    Sonnet-Reader pro Tag `overnight_after_day` und `layover_iata` korrekt rät.
    Genau das ist der dokumentierte offene Bug ("ZeroDay-Stochastik: Sonnet
    verliert die Landing-Time"). Die BMF-Regel ist aber rein zeitlich:
    Maßgeblich für den Verpflegungsmehraufwand ist das LAND, in dem man sich um
    24:00 ORTSZEIT befindet.

ANSATZ:
    Aus den (in UTC vorliegenden — siehe CAS "Alle zeiten in UTC") Flug-Zeiten
    und der echten Flughafen-Zeitzone (airport_tz, ~11k Airports) rechnen wir
    deterministisch:
      - das LOKALE Ankunftsdatum eines Flugs (Mitternachts-Übergänge!),
      - den Übernachtungsort pro Kalendertag,
      - ob ein Heimflug ein Nachtflug ist (Take-off abends im Ausland,
        Landung am Folgetag) → voller Auslandstag statt An/Ab.

    KEINE externe tz-Library nötig — Python `zoneinfo` (stdlib ab 3.9).

Reines Datenmodul + Funktionen, voll testbar. Ändert für sich genommen nichts
am Live-Ergebnis; wird in normalized_tours hinter einem Flag als Audit/Override
verwendet.
"""
from __future__ import annotations

from datetime import datetime, timedelta, date as _date
from typing import Any, Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
    _HAVE_ZONEINFO = True
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore
    _HAVE_ZONEINFO = False

try:
    from airport_tz import airport_tz as _airport_tz, airport_country as _airport_country
except Exception:  # pragma: no cover
    _airport_tz = None  # type: ignore
    _airport_country = None  # type: ignore


def _parse_hhmm(hhmm: Optional[str]) -> Optional[Tuple[int, int]]:
    """'13:05' → (13, 5). Toleriert None/'' und Sekunden."""
    if not hhmm or not isinstance(hhmm, str):
        return None
    s = hhmm.strip()
    if not s or ':' not in s:
        return None
    parts = s.split(':')
    try:
        h = int(parts[0]); m = int(parts[1])
    except (ValueError, IndexError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return (h, m)


def _utc_instant(datum: str, hhmm_utc: str) -> Optional[datetime]:
    """Baut ein UTC-datetime aus Datum (YYYY-MM-DD) + UTC-Wanduhr 'HH:MM'."""
    hm = _parse_hhmm(hhmm_utc)
    if hm is None:
        return None
    try:
        d = datetime.strptime(datum, '%Y-%m-%d')
    except (ValueError, TypeError):
        return None
    return d.replace(hour=hm[0], minute=hm[1], tzinfo=ZoneInfo('UTC')) if _HAVE_ZONEINFO else None


def local_datetime(datum: str, hhmm_utc: str, iata: str) -> Optional[datetime]:
    """UTC-Wanduhr an einem Datum → LOKALES datetime am Flughafen `iata`.

    Gibt None zurück wenn TZ/Zeit nicht auflösbar (Aufrufer behält dann das
    bisherige Marker-Verhalten — rein additiv).
    """
    if not (_HAVE_ZONEINFO and _airport_tz):
        return None
    inst = _utc_instant(datum, hhmm_utc)
    if inst is None:
        return None
    tzname = _airport_tz(iata)
    if not tzname:
        return None
    try:
        return inst.astimezone(ZoneInfo(tzname))
    except Exception:
        return None


def local_arrival_date(
    dep_datum: str, dep_hhmm_utc: str, dep_iata: str,
    arr_hhmm_utc: str, arr_iata: str,
) -> Optional[str]:
    """Lokales Ankunfts-DATUM (YYYY-MM-DD) am Zielflughafen.

    Berücksichtigt, dass die Ankunfts-UTC-Uhrzeit < Abflug-UTC-Uhrzeit bedeutet,
    dass über UTC-Mitternacht geflogen wurde (Ankunft am Folge-UTC-Tag); danach
    wird in Ortszeit umgerechnet (kann nochmals einen Tag verschieben, z.B. nach
    Asien/Pazifik).
    """
    dep = _parse_hhmm(dep_hhmm_utc)
    arr = _parse_hhmm(arr_hhmm_utc)
    if dep is None or arr is None:
        return None
    dep_inst = _utc_instant(dep_datum, dep_hhmm_utc)
    if dep_inst is None:
        return None
    arr_min = arr[0] * 60 + arr[1]
    dep_min = dep[0] * 60 + dep[1]
    arr_inst = dep_inst
    # gleiche oder spätere UTC-Uhrzeit = selber UTC-Tag, sonst +1 UTC-Tag
    add_days = 0 if arr_min >= dep_min else 1
    arr_inst = dep_inst + timedelta(days=add_days)
    arr_inst = arr_inst.replace(hour=arr[0], minute=arr[1])
    if not (_HAVE_ZONEINFO and _airport_tz):
        return arr_inst.date().isoformat()
    tzname = _airport_tz(arr_iata)
    if not tzname:
        return arr_inst.date().isoformat()
    try:
        local = arr_inst.astimezone(ZoneInfo(tzname))
        return local.date().isoformat()
    except Exception:
        return arr_inst.date().isoformat()


def is_night_return_flight(
    dep_datum: str, dep_hhmm_utc: str, dep_iata: str,
    arr_hhmm_utc: str, arr_iata: str,
) -> Optional[bool]:
    """True wenn ein (Heim-)Flug über lokale Mitternacht des ABFLUGORTS geht —
    d.h. der Crew-Member war an `dep_datum` bis 24:00 Ortszeit noch im Ausland.

    Das ist die BMF-konforme, zeitbasierte Variante des bisherigen
    `overnight_after_day`-Heuristik-Flags (R23 Bug 2). Gibt None wenn nicht
    auflösbar (dann gilt weiter das Marker-Flag).
    """
    dep_local = local_datetime(dep_datum, dep_hhmm_utc, dep_iata)
    if dep_local is None:
        return None
    arr_local_date = local_arrival_date(
        dep_datum, dep_hhmm_utc, dep_iata, arr_hhmm_utc, arr_iata,
    )
    if arr_local_date is None:
        return None
    # Lokales Abflugdatum am Abflugort
    dep_local_date = dep_local.date().isoformat()
    # Nachtflug: Ankunft (am Zielort, lokal) liegt nach dem Abflug-Ortsdatum
    return arr_local_date > dep_local_date


def overnight_country_for_day(day: Dict[str, Any], homebase: str) -> Optional[Dict[str, Any]]:
    """Bestimmt deterministisch das Land um 24:00 Ortszeit für einen CAS-Tag.

    Nutzt layover/destination/origin-IATA + airport_tz. Gibt
    {'iso': 'IN', 'iata': 'BLR', 'is_foreign': True, 'source': '...'} zurück
    oder None, wenn nichts Belastbares ableitbar ist.

    Dies ist eine ZUSÄTZLICHE Evidence-Quelle (Audit), die auch dann ein Land
    liefert, wenn die BMF-Tabelle den Airport nicht kennt.
    """
    if not _airport_country:
        return None
    hb = (homebase or 'FRA').upper().strip()

    # Reihenfolge: wo schläft die Person? layover > destination > next_layover
    for key in ('layover_iata', 'destination_iata', 'next_layover_iata'):
        iata = (day.get(key) or '').upper().strip()
        if len(iata) == 3 and iata.isalpha() and iata != hb:
            iso = _airport_country(iata)
            if iso:
                return {
                    'iso': iso, 'iata': iata,
                    'is_foreign': iso != 'DE',
                    'source': f'tz_midnight.{key}',
                }
    return None
