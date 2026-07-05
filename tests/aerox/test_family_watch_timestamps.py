"""
Family-Watch Zeitstempel-Kanonisierung (Audit 2026-07-05, Bereich 6).

Vorher gingen SB-Rohwerte mit str(...)[:25] in den Status-Payload — bei
Mikrosekunden ('…T10:30:00.123456+00:00') schnitt das den TZ-Offset ab und
liess verstuemmelte Bruchteile stehen; last_seen_iso kam als naiv-lokales
isoformat() mit Mikrosekunden. Jetzt: EIN kanonisches API-Format
'YYYY-MM-DDTHH:MM:SSZ' (UTC, sekundengenau) via _iso_utc_z/_now_utc_z.

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/aerox/test_family_watch_timestamps.py -v
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from blueprints.family_watch import _iso_utc_z, _now_utc_z, _parse_iso  # noqa: E402

CANON = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


# ── _iso_utc_z: kanonische Ausgabe ───────────────────────────────────────────

def test_microseconds_plus_offset_normalized():
    """DER Audit-Fall: SB-timestamptz mit Mikrosekunden — [:25] haette
    '2026-07-05T10:30:00.12345' (TZ weg, Bruchteil zerhackt) geliefert."""
    out = _iso_utc_z("2026-07-05T10:30:00.123456+00:00")
    assert out == "2026-07-05T10:30:00Z"


def test_nonzero_offset_converted_to_utc():
    out = _iso_utc_z("2026-07-05T12:30:00.987654+02:00")
    assert out == "2026-07-05T10:30:00Z"


def test_z_input_stays_z():
    assert _iso_utc_z("2026-07-05T10:30:00Z") == "2026-07-05T10:30:00Z"


def test_naive_treated_as_utc():
    # Gleiche Semantik wie _parse_iso: naiv == UTC (Cloud Run laeuft UTC).
    assert _iso_utc_z("2026-07-05T10:30:00.123456") == "2026-07-05T10:30:00Z"


def test_postgres_space_separator():
    # Postgres-Textform 'YYYY-MM-DD HH:MM:SS+00' parst fromisoformat (3.12).
    assert _iso_utc_z("2026-07-05 10:30:00+00:00") == "2026-07-05T10:30:00Z"


def test_none_stays_none_and_empty_stays_none():
    assert _iso_utc_z(None) is None
    assert _iso_utc_z("") is None
    assert _iso_utc_z("   ") is None


def test_unparseable_passthrough_untruncated():
    """Nie mitten im String choppen: Unparsebares geht UNGEKUERZT durch
    (kein [:25], das z.B. einen Multibyte-/Offset-Rest zerschneidet)."""
    weird = "kein-zeitstempel-aber-laenger-als-25-zeichen-ümläute"
    assert _iso_utc_z(weird) == weird


def test_datetime_object_input():
    d = dt.datetime(2026, 7, 5, 12, 30, 0, 123456,
                    tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert _iso_utc_z(d) == "2026-07-05T10:30:00Z"


# ── _now_utc_z: Schreibformat ────────────────────────────────────────────────

def test_now_utc_z_is_canonical_and_current():
    s = _now_utc_z()
    assert CANON.match(s), s
    parsed = _parse_iso(s)
    now = dt.datetime.now(dt.timezone.utc)
    assert abs((now - parsed).total_seconds()) < 60


# ── Regression: die Status-Emitter nutzen den Kanon (kein [:25]-Chop mehr) ──

def test_status_source_has_no_25_chop():
    src_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))),
        "blueprints", "family_watch.py")
    with open(src_path, encoding="utf-8") as f:
        src = f.read()
    assert "[:25]" not in src, (
        "family_watch.py enthaelt wieder einen [:25]-Zeitstempel-Chop — "
        "bitte _iso_utc_z benutzen (Audit 2026-07-05)")
