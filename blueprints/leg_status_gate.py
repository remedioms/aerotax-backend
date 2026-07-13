"""Leg-Status Plausibilitäts-/Monotonie-Gate — FlightState-Härtung für die
Roster-/Kalender-Sektor-Fläche (`_enrich_leg_delays`).

WARUM (Owner-/Fable-Task 2026-07-13, Fall (a) LH454→SFO):
Der Dual-Side-Merge `_flight_obs_merged` liefert einen ROHEN Board-Status-String
(`status`, arr-Seite gewinnt). Diesen schrieb `_enrich_leg_delays` bislang
UNGEGATET pro `ical_sectors[]`-Leg (`sec['status'] = m.get('status')`). Manche
Boards flippen für eine Flugnummer fälschlich früh auf „gelandet HH:MM" — bei
einem 11-h-Langstreckenflug (FRA→SFO) kann der Flieger aber physikalisch nicht
schon 13:03 gelandet sein. Der Rohstatus floss additiv in Kalender-Leg-Anzeige,
Feed-Bordkarte UND in `flights_live[].status` (get_friends_today) → dort log der
Freund „gelandet", während er nachweislich noch flog.

Die FlightState-Engine (blueprints/flight_state.py) verwirft eine solche
unmögliche Landung strukturell über Airborne-Gate/Monotonie/Physik. Diese
Fläche fährt jetzt DIESELBE Wahrheit: der Rohstatus wird nur durchgereicht, wenn
er plausibel ist — ein TERMINALER („landed"-Bucket) Status darf erst gelten, wenn
die früheste physikalisch mögliche Ankunft erreicht ist. Sonst wird der terminale
Status verworfen (auf None gesetzt) statt eine erfundene Landung zu behaupten.

DESIGN-PRINZIP (konservativ, additiv, keine erfundenen Daten):
- Wir ERFINDEN nie einen Status. Wir UNTERDRÜCKEN nur einen beweisbar
  unmöglichen terminalen Status.
- Fehlen die Belege (kein sched_arr, keine dep-Zeit, keine Geo-Koordinaten,
  unparsebare Zeiten), gilt „fail-open": der Rohstatus bleibt unangetastet —
  das Gate richtet sich NUR gegen den nachweisbar-unmöglichen Fall.
- Reine Funktion, kein I/O. Fixture-testbar. Wirft nie.

Diese Datei ist die schmale Ergänzung; die Engine-Ableitung (phase/phase_conf)
läuft parallel im Aufrufer wie bei crew_state/flights_live (r111–r114).
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Optional


# Board-Status-Tokens, die eine ABGESCHLOSSENE Ankunft behaupten (Teilmenge des
# app._FLIGHT_LANDED_STATES — hier lokal gehalten, damit das Gate ohne app-Import
# testbar bleibt). Substring-Match auf lowercase, wie _flight_status_bucket.
_TERMINAL_LANDED_TOKENS = (
    "landed", "arrived", "at gate", "on ground", "on blocks", "on-blocks",
    "gelandet", "angekommen", "baggage", "gepäck", "gepaeck",
)

# Maximale EFFEKTIVE Grundgeschwindigkeit inkl. Steig-/Sinkflug + Taxi-Overhead,
# in km/h. Bewusst GROSSZÜGIG (schneller als real) gewählt: das Gate soll NUR den
# krass-unmöglichen Fall fangen (Landung Stunden zu früh), nie einen knappen
# Grenzfall fälschlich verwerfen. ~950 km/h ≈ 513 kt Block-Schnitt ist für keinen
# Linienjet real erreichbar → wer davor „landet", tut es unmöglich.
_MAX_EFF_GROUND_KMH = 950.0

# Fixer Boden-Overhead (min): Taxi-out + Taxi-in. Verkürzt die früheste mögliche
# Ankunft NICHT — es ist additiver Puffer NACH oben (macht das Gate strenger,
# also konservativer im Verwerfen? Nein — mehr Overhead ⇒ spätere früheste
# Ankunft ⇒ Board-„landed" wird EHER unplausibel). Klein gehalten, damit echte
# Kurzstrecken (FRA→MUC) nicht fälschlich als „zu früh gelandet" gelten.
_GROUND_OVERHEAD_MIN = 12.0

# Slack (min) VOR der frühesten möglichen Ankunft, ab dem ein „landed" toleriert
# wird. −15 min laut Task: ein reales Board darf ein paar Minuten „vorlaufen"
# (Runway-Touchdown vs. On-Block-Meldung). Nur wer DEUTLICH früher „landet",
# wird verworfen.
_LANDED_SLACK_MIN = 15.0


def _parse_iso_utc(iso) -> Optional[float]:
    """UTC-ISO ('...Z'/Offset/naiv=UTC) → Epoch-Sekunden oder None. Wirft nie."""
    if iso is None:
        return None
    s = str(iso).strip()
    if not s:
        return None
    if s.endswith("Z") or s.endswith("z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _gc_km(a_ll, b_ll) -> Optional[float]:
    """Great-Circle-km zwischen zwei (lat,lon)-Tupeln, None wenn eins fehlt."""
    if not a_ll or not b_ll:
        return None
    try:
        lat1, lon1 = float(a_ll[0]), float(a_ll[1])
        lat2, lon2 = float(b_ll[0]), float(b_ll[1])
    except (TypeError, ValueError, IndexError):
        return None
    rl1, rl2 = math.radians(lat1), math.radians(lat2)
    dlat = rl2 - rl1
    dlon = math.radians(lon2 - lon1)
    h = math.sin(dlat / 2) ** 2 + math.cos(rl1) * math.cos(rl2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def is_terminal_landed(status) -> bool:
    """True, wenn der rohe Board-Status eine abgeschlossene Ankunft behauptet
    ('gelandet 13:03', 'Arrived', 'at gate', 'baggage', …). Substring-Match auf
    lowercase — deckungsgleich mit app._flight_status_bucket → 'landed'."""
    s = str(status or "").strip().lower()
    if not s:
        return False
    return any(t in s for t in _TERMINAL_LANDED_TOKENS)


def earliest_possible_arrival_ts(dep_ts: Optional[float],
                                 dep_ll, arr_ll) -> Optional[float]:
    """Früheste physikalisch mögliche Ankunft (Epoch), abgeleitet aus effektivem
    Abflug + Großkreis-Distanz / max. eff. Grundgeschwindigkeit + Boden-Overhead.
    None, wenn dep_ts oder eine Koordinate fehlt (dann greift das sched_arr-Gate
    allein, sonst fail-open)."""
    if dep_ts is None:
        return None
    dist_km = _gc_km(dep_ll, arr_ll)
    if dist_km is None:
        return None
    flight_h = dist_km / _MAX_EFF_GROUND_KMH
    return dep_ts + flight_h * 3600.0 + _GROUND_OVERHEAD_MIN * 60.0


def landed_status_plausible(status, *, now: Optional[float] = None,
                            sched_arr_iso: Optional[str] = None,
                            est_arr_iso: Optional[str] = None,
                            dep_ts: Optional[float] = None,
                            dep_ll=None, arr_ll=None) -> bool:
    """Darf ein TERMINALER ('landed'-Bucket) Board-Status als wahr gelten?

    Regel (physikalische Mindest-Flugzeit + Fahrplan-Untergrenze):
      terminal 'landed' ist NUR plausibel, wenn `now` mindestens die früheste
      der beiden Schranken minus `_LANDED_SLACK_MIN` erreicht hat:
        1. früheste physikalisch mögliche Ankunft (est_dep + GC-Distanz / v_max
           + Boden-Overhead) — falls dep_ts + Koordinaten vorliegen,
        2. Fahrplan-Ankunft (est_arr bevorzugt, sonst sched_arr, in echt-UTC)
           minus Slack — falls vorhanden.
      Ein 'landed' VOR beiden erreichbaren Schranken ist physikalisch unmöglich
      → nicht plausibel.

    FAIL-OPEN: liegt KEINE der Schranken vor (weder Zeiten noch Koordinaten),
    True (Rohstatus unangetastet) — das Gate verwirft nur nachweisbar Unmögliches.
    Wirft nie."""
    if not is_terminal_landed(status):
        return True                       # kein terminaler Status → nichts zu gaten
    now = now if now is not None else time.time()

    # Schranke 2: Fahrplan-/Ist-Ankunft (bereits echt-UTC vom Aufrufer).
    sched_ts = _parse_iso_utc(est_arr_iso) or _parse_iso_utc(sched_arr_iso)

    # Schranke 1: physikalische Mindest-Ankunft.
    phys_ts = earliest_possible_arrival_ts(dep_ts, dep_ll, arr_ll)

    bounds = [b for b in (sched_ts, phys_ts) if b is not None]
    if not bounds:
        return True                       # keine Belege → fail-open (nie erfinden)

    # DIE UNTERE Schranke: „frühestens" = das kleinere der Belege. Ein Board darf
    # zumindest bis dahin (minus Slack) NICHT „gelandet" behaupten.
    earliest = min(bounds)
    return now >= (earliest - _LANDED_SLACK_MIN * 60.0)


def gated_leg_status(status, *, now: Optional[float] = None,
                     sched_arr_iso: Optional[str] = None,
                     est_arr_iso: Optional[str] = None,
                     dep_ts: Optional[float] = None,
                     dep_ll=None, arr_ll=None):
    """Rohstatus → plausibilitäts-gegateter Status.

    Gibt den Rohstatus 1:1 zurück, AUSSER wenn er einen physikalisch unmöglichen
    terminalen 'landed' behauptet — dann None (ehrlich „kein terminaler Status",
    statt eine erfundene Landung zu propagieren). Nicht-terminale Status
    (airborne/delayed/boarding/…) laufen IMMER unverändert durch."""
    if landed_status_plausible(status, now=now, sched_arr_iso=sched_arr_iso,
                               est_arr_iso=est_arr_iso, dep_ts=dep_ts,
                               dep_ll=dep_ll, arr_ll=arr_ll):
        return status
    return None
