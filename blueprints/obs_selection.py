"""Geteilte Row-/Instanz-Selektion für die ZWILLINGS-Resolver (2026-07-16).

WARUM (Owner/Fable, Struktur-Kur Punkt 7): die beiden Board-Fakten-Resolver
`_flight_obs_merged` (app.py) und `_flight_facts_from_obs`
(blueprints/aerox_data_blueprint.py) interpretieren dieselben
`airport_delay_obs`-Rows, hatten aber JE EIGENE Row-/Instanz-/Status-Logik. In
der Juli-Nacht wurden dieselben drei Regeln DREIMAL einzeln geflickt (bare-Repoll,
Instanz-Toleranz, esti-Vorrang, ARR-Müll-Scrub). Diese Divergenz ist die Wurzel
der zersplitterten Rohdaten-Interpretation.

HIER leben die drei PUREN Selektions-Regeln jetzt GENAU EINMAL, damit beide
Resolver dieselbe Wahrheit importieren (kein zweiter Ort mehr, an dem sie
auseinanderlaufen können):

  1. „Row mit esti schlägt bare Row derselben Instanz"  → `row_richness`
  2. „Folgetags-Row = fremde Instanz"                   → `same_instance_sched`
  3. „ARR-Row nur mit ankunfts-plausiblem Status"        → `arr_status_plausible`

PUR & OHNE I/O: keine DB-/Netz-Zugriffe, wirft nie. Reiner Vergleich auf
Row-dicts (airport_delay_obs-Shape: sched/esti/gate/max_delay_min/status).

MIGRATION: aerox_data_blueprint importiert diese Helfer bereits (byte-identisches
Verhalten — die Konstanten/Funktionen sind 1:1 hierher gezogen). app.py-Seite
(`_flight_obs_merged._obs_lookup`) übernimmt sie im nächsten Schritt (s. Plan im
Rückgabe-Bericht) — dann existiert die ARR-Müll-/Instanz-Regel auch dort GENAU
EINMAL statt implizit gar nicht.
"""
import re as _re

# Soll-Zeit-Toleranz (min): zwei airport_delay_obs-Rows gehören zur SELBEN Tages-
# Instanz eines Flugs nur, wenn ihre Soll-Zeit (`sched`) höchstens so weit
# auseinanderliegt. Weicht die `sched` einer Row stärker ab, ist es eine FREMDE
# Instanz (Folgetags-Repoll unter falschem Datum, andere Soll-Zeit) — sie darf die
# Basis-/Zeit-Row NICHT verdrängen und ihren Status NICHT draufmergen (Owner/Fable
# 2026-07-16, FIX A/C: LH867/LH1126 Folgetags-Row verunreinigte den Vortag).
OBS_SAME_INSTANCE_SCHED_TOL_MIN = 45

# Ankunfts-plausible Status-Signalwörter (kleingeschrieben, Substring). NUR diese
# dürfen von einer ARR-Row (‹Ziel›#ARR) als Merge-Status akzeptiert werden — ein
# abflug-typischer Status ('Boarding'/'Gate…'/'Abgeflogen') auf einer ARR-Row ist
# Scraper-Feld-Müll (LH1126 BCN#ARR trug 'Boarding') und wird ignoriert.
OBS_ARR_PLAUSIBLE_STATUS = (
    'gelandet', 'landed', 'arrived', 'angekommen', 'at gate', 'on blocks',
    'on-blocks', 'gepäck', 'gepaeck', 'baggage', 'diverted', 'umgeleitet',
    'cancelled', 'canceled', 'annulliert', 'gestrichen', 'delayed', 'verspätet',
    'verspaetet', 'estimated', 'erwartet', 'approach', 'anflug', 'final',
)

_SCHED_RE = _re.compile(r'(\d{1,2}):(\d{2})')


def obs_sched_min(row):
    """Soll-Zeit einer Obs-Row als Minuten-seit-Mitternacht (0..1439), oder None.
    Reine bare-'HH:MM'/ISO-Toleranz-Hilfe — vergleicht nur die Wanduhr-Minute,
    keine TZ (beide Rows stehen in derselben Stations-Ortszeit). Wirft nie."""
    s = (row.get('sched') or '').strip() if isinstance(row, dict) else ''
    if not s:
        return None
    m = _SCHED_RE.search(s)
    if not m:
        return None
    try:
        return int(m.group(1)) * 60 + int(m.group(2))
    except Exception:
        return None


def same_instance_sched(row, ref_row):
    """True, wenn `row` zur SELBEN Tages-Instanz wie `ref_row` gehört (Soll-Zeit
    innerhalb OBS_SAME_INSTANCE_SCHED_TOL_MIN). Fehlt eine der Soll-Zeiten →
    True (fail-open: nicht als fremd verwerfen, wenn wir es nicht sicher wissen).
    Mitternachts-Wrap (23:55 vs 00:05) wird als klein behandelt."""
    a = obs_sched_min(row)
    b = obs_sched_min(ref_row)
    if a is None or b is None:
        return True
    diff = abs(a - b)
    diff = min(diff, 1440 - diff)      # zyklisch (Mitternacht)
    return diff <= OBS_SAME_INSTANCE_SCHED_TOL_MIN


def arr_status_plausible(status):
    """True, wenn `status` ein ANKUNFTS-plausibler Board-Status ist (darf von
    einer ARR-Row als Merge-Status akzeptiert werden). Ein dep-typisches
    'Boarding'/'Gate…'/'Abgeflogen' auf einer ARR-Row ist Scraper-Müll → False.
    Leerer Status → False (kein Signal). Wirft nie."""
    s = str(status or '').strip().lower()
    if not s:
        return False
    return s in OBS_ARR_PLAUSIBLE_STATUS or any(
        t in s for t in OBS_ARR_PLAUSIBLE_STATUS)


def row_richness(row, *, arr=False):
    """Informationslage einer Obs-Row als vergleichbare Zahl (höher = reicher).
    Die Ist-Zeit (`esti`) ist das Wertvollste — eine nackte, spätere Repoll-Row
    (nur status='Geplant', esti leer) darf die esti-Row derselben Instanz NIE
    verdrängen (LH867). Reihenfolge: esti ≫ delay-Zahl > gate > sched.

    `arr=True` skaliert die Gewichte wie die bestehende ARR-Paarung
    (`_best_arr_for_dep`: esti=8, delay=4, gate=2, sched=1); Default wie `_best`
    (esti=4, delay=2, gate=1, sched=1). Beide Skalen sind ordnungserhaltend
    (esti dominiert), sodass jeder Aufrufer seine bisherige Selektion behält."""
    if not isinstance(row, dict):
        return 0
    has_esti = bool((row.get('esti') or '').strip())
    has_delay = row.get('max_delay_min') is not None
    has_gate = bool((row.get('gate') or '').strip())
    has_sched = bool((row.get('sched') or '').strip())
    if arr:
        return ((8 if has_esti else 0) + (4 if has_delay else 0)
                + (2 if has_gate else 0) + (1 if has_sched else 0))
    return ((4 if has_esti else 0) + (2 if has_delay else 0)
            + (1 if has_gate else 0) + (1 if has_sched else 0))
