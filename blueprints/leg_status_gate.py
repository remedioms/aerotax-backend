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

    # Schranke 1 (HART, absolut): physikalische Mindest-Ankunft = eff. Abflug +
    # Großkreis / v_max + Boden-Overhead. Kein Board-Zeitstempel kann sie
    # unterbieten — ein Flug ist NIE vor ihr gelandet.
    phys_ts = earliest_possible_arrival_ts(dep_ts, dep_ll, arr_ll)

    # Schranke 2 (PROXY): Fahrplan-/Ist-Ankunft (bereits echt-UTC vom Aufrufer),
    # Ist (est_arr) vor Plan (sched_arr). Nur ein grober Sanity-Proxy für den Fall,
    # dass die Physik fehlt (kein dep_ts / keine Koordinaten) — der Fahrplan ist
    # keine echte untere Landeschranke (ein Flug kann vor Plan landen).
    sched_ts = _parse_iso_utc(est_arr_iso) or _parse_iso_utc(sched_arr_iso)

    # Liegt der HARTE Physik-Boden vor, ist ER allein maßgeblich. Ein stale/
    # vortägiger est_arr darf ihn NICHT unterlaufen (Owner 2026-07-13: LH454
    # FRA→SFO stand +185 min verspätet noch in FRA, aber esti_arr trug die
    # Ankunft von GESTERN, 2026-07-12T13:03−07:00 = längst < now). Früher nahm
    # das Gate min(sched/est, phys) → dieser stale-frühe Wert zog die Schranke
    # unter den Physik-Boden und die Geister-Landung „Arrived" schlüpfte durch.
    if phys_ts is not None:
        return now >= (phys_ts - _LANDED_SLACK_MIN * 60.0)

    # Kein Physik-Boden (keine dep-Zeit/Koordinaten) → Proxy nutzen, sonst
    # fail-open (nie eine Landung erfinden, nur nachweisbar Unmögliches fangen).
    if sched_ts is None:
        return True
    return now >= (sched_ts - _LANDED_SLACK_MIN * 60.0)


# ── INSTANZ-FENSTER EINES LEGS (Sweep-Befund 2026-08-09, LH712 FRA→ICN) ─────
# EINE Quelle für die beiden Schwellen, die das Projekt für „gehört dieser
# Beleg zu DIESER Tages-Instanz?" benutzt. app._DEP_EARLY_MARGIN_H /
# app._DEP_LATE_MARGIN_H lesen sie von hier, damit nicht drei verschiedene
# Zahlen entstehen.
#   EARLY 6 h  — kein Linienflug geht 6 h VOR Plan raus.
#   LATE 20 h  — jenseits davon ist es keine Verspätung mehr, sondern eine
#                Umplanung bzw. der 24-h-Nachbar derselben täglichen Nummer.
DEP_EARLY_MARGIN_H = 6
DEP_LATE_MARGIN_H = 20


def live_pos_same_instance(seen_ts, sched_dep_iso, *,
                           early_h: float = DEP_EARLY_MARGIN_H,
                           late_h: float = DEP_LATE_MARGIN_H) -> bool:
    """Kann ein LIVE-POSITIONS-Snapshot (`aircraft_live`, Beobachtungszeit
    `seen_ts`) zum Leg mit Soll-Abflug `sched_dep_iso` gehören?

    WARUM (Prod-Beleg, gelesen 2026-08-08T21:09Z): `aircraft_live` keyt einen
    Snapshot NUR über Flugnummer/Funkname/Reg + Ziel — es gibt dort KEIN Datum.
    Bei einer täglich fliegenden Langstrecke ist die Maschine von GESTERN zum
    Abfrage-Zeitpunkt noch in der Luft und matcht die Zeile von MORGEN exakt:
    `flight=LH712, dest=ICN, on_ground=false, seen_ts 2026-08-08T21:01:09Z`
    (D-AIXB über Xinjiang) klebte am Roster-Leg LH712 FRA→ICN mit Soll-Abflug
    2026-08-09T13:35:00Z — 16,6 h VOR dessen Abflug. Über die FlightState-Engine
    (T3: Position + Kinematik ⇒ AIRBORNE, `phase_conf=observed`) wurde daraus
    `status='airborne'` an einem Flug, der noch gar nicht gestartet war.
    Der vorhandene Riegel `app._obs_dep_same_instance` konnte das nicht fangen:
    er prüft Board-/Warehouse-Rows über deren `date`/`sched`-Spalten — ein
    Positions-Snapshot hat beide nicht und lief nie durch ihn.

    REGEL: der Snapshot muss im Instanz-Fenster [sched_dep − 6 h, sched_dep +
    20 h] liegen — dieselben Schwellen wie `_gate_facts_dep_against_leg` /
    `_lh_facts_same_instance`. Ein wirklich fliegender Flug liegt IMMER darin
    (Off-Block frühestens kurz vor Plan, längster Linienflug < 20 h), auch ein
    stark verspäteter.

    FAIL-OPEN: fehlt `seen_ts` ODER `sched_dep_iso` oder ist eins unparsbar,
    True — keine Evidenz ⇒ exakt bisheriges Verhalten. Pure, wirft nie."""
    try:
        if seen_ts is None or sched_dep_iso is None:
            return True
        if isinstance(seen_ts, (int, float)):
            seen = float(seen_ts)
            if seen > 4102444800:                 # ms-Epoch heuristisch
                seen /= 1000.0
        else:
            seen = _parse_iso_utc(seen_ts)
        if isinstance(sched_dep_iso, datetime):
            _d = sched_dep_iso
            if _d.tzinfo is None:                 # naiv = UTC (nie Lokalzeit)
                _d = _d.replace(tzinfo=timezone.utc)
            dep = _d.timestamp()
        else:
            dep = _parse_iso_utc(sched_dep_iso)
        if seen is None or dep is None:
            return True
        return (dep - early_h * 3600.0) <= seen <= (dep + late_h * 3600.0)
    except Exception:
        return True


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
