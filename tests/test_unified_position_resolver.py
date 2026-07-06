# ═══════════════════════════════════════════════════════════════
#  Unified Position Resolver — EINE Positions-Quelle für ALLE
#  „wo ist der Flieger"-Fragen (blueprints.warehouse_reader.position_for_flight)
#
#  Owner-Ziel (2026-07-06): eigene Live-Position, „nächster Flieger",
#  Family-„fliegt gerade" und Freunde/Crew-Radar dürfen sich nie mehr
#  widersprechen — sie ziehen alle aus GENAU EINER Kaskade. Diese Suite
#  beweist die harten Contract-Punkte:
#
#   (a) fr24_live-Tabelle liefert eine Position OHNE jeden Extern-Call.
#   (b) Auswahl nach FRISCHE: frischere aircraft_positions gewinnt über
#       ältere fr24_live (und umgekehrt); Rang bricht nur Gleichstände.
#   (c) Family (allow_paid=False) fasst NIE den bezahlten Tier (AeroDataBox) an.
#   (d) FR24-Selbst-Harvest im User-Pfad ist AUS (Kill-Switch-Default
#       FR24_BACKEND_SELFHARVEST=0); der reine Store-Read funktioniert trotzdem.
#   (e) targeted + alle Tabellen leer → GENAU EIN budget-gedeckelter
#       AeroDataBox-Versuch (der einzige erlaubte Extern-Zugriff).
#
#  KEIN echter Netz-/DB-Zugriff: Supabase-Client, der Tabellen-Backfill und
#  alle externen Helfer werden gemockt.
# ═══════════════════════════════════════════════════════════════
import os
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import time
from unittest.mock import MagicMock

import pytest

import app  # noqa: F401 — Blueprint-Registrierung vor Direkt-Import
import blueprints.adsb_blueprint as ADSB
from blueprints import warehouse_reader as WR

# Echte (unmodifizierte) _fetch_fr24 beim Import festhalten — die autouse-Fixture
# überschreibt das Modul-Attribut pro Test mit einem inerten Mock; die
# Kill-Switch-Tests brauchen die Original-Funktion zurück.
_real_fetch_fr24 = ADSB._fetch_fr24


# ─── Helfer: OpenSky-State-Row bauen; row[3] = ECHTE Beobachtungszeit ─────────
def _row(hex="4d0113", cs="CLX4327", reg="LX-VCJ", obs_ts=1000.0,
         lat=30.37, lon=104.74):
    """Minimale OpenSky-State-Array-Row. row[3]=time_position (obs_ts),
    row[5]=lon, row[6]=lat, row[2]=reg — genau das Layout, das die Kaskade liest."""
    r = [None] * 17
    r[0] = hex
    r[1] = cs
    r[2] = reg
    r[3] = obs_ts
    r[5] = lon
    r[6] = lat
    r[8] = False
    return r


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Jeden Test gegen echten Netz-/DB-/Store-Zustand isolieren.

    Defaults = „alle Quellen leer": fr24 miss, aircraft_positions miss, freier
    ADS-B-Mirror miss, AeroDataBox nicht angefasst. Jeder Test überschreibt nur
    die eine Quelle, die er prüft. So kann kein Test versehentlich extern gehen.
    """
    # FR24-In-Memory-Store leeren (Tier 1 arbeitet darauf).
    ADSB._FR24["entries"].clear()
    ADSB._FR24["by_cs"].clear()
    ADSB._FR24["store_at"] = 0.0
    ADSB._FR24["store_fresh_at"] = 0.0
    ADSB._FR24["last_at"] = 0.0

    # Kill-Switch NICHT gesetzt = Default (Selbst-Harvest AUS). Falls die
    # Umgebung ihn gesetzt hat, für die Testdauer entfernen.
    monkeypatch.delenv('FR24_BACKEND_SELFHARVEST', raising=False)

    # Tier 1 + 2: Tabellen sauber leer (per Default). Kein Supabase.
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: None)
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb', lambda h: None)
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (None, False))

    # Tier 3 + 4: externe Helfer als MagicMock — Default „kein Signal", damit
    # jeder Test per assert_not_called beweisen kann, dass NICHT extern gegangen
    # wurde. Wer Tier 3/4 prüft, überschreibt den return_value gezielt.
    monkeypatch.setattr(ADSB, '_fetch_adsb_lol', MagicMock(return_value=None))
    monkeypatch.setattr(ADSB, '_adb_position_attempt',
                        MagicMock(return_value=(None, None, 'no_data')))
    yield
    ADSB._FR24["entries"].clear()
    ADSB._FR24["by_cs"].clear()


# ─── (a) fr24_live liefert Position OHNE Extern-Call ─────────────────────────
def test_fr24_table_serves_position_without_external_call(monkeypatch):
    fr = _row(obs_ts=5000.0)
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", callsign="CLX4327", targeted=True, allow_paid=True)

    assert source == "fr24"
    assert row is fr
    assert obs_ts == 5000.0                      # ECHTER obs_ts aus row[3]
    # Der springende Punkt: KEINE externe Quelle wurde angefasst, obwohl
    # targeted=True/allow_paid=True beide Extern-Tiers freigeschaltet hätten.
    ADSB._fetch_adsb_lol.assert_not_called()
    ADSB._adb_position_attempt.assert_not_called()
    # Diagnose bestätigt den ausgewählten Tier.
    assert any(t.get("selected") == "fr24" and t.get("upstream") == "fr24"
               for t in tried)


# ─── (b) Auswahl nach FRISCHE, nicht nach Rang ────────────────────────────────
def test_fresher_aircraft_positions_beats_older_fr24(monkeypatch):
    fr = _row(obs_ts=1000.0, reg="LX-OLD")            # alt
    ap = _row(obs_ts=2000.0, reg="D-FRESH")           # frischer
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb',
                        lambda h: {"row": ap, "fetched_at": 2000.0})

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)

    # Frischerer echter Fix gewinnt — obwohl fr24 der ranghöhere Tier 1 ist.
    assert source == "aircraft_positions"
    assert obs_ts == 2000.0
    assert row[2] == "D-FRESH"
    ADSB._fetch_adsb_lol.assert_not_called()
    ADSB._adb_position_attempt.assert_not_called()


def test_fresher_fr24_beats_older_aircraft_positions(monkeypatch):
    fr = _row(obs_ts=3000.0, reg="LX-FRESH")          # frischer
    ap = _row(obs_ts=2000.0, reg="D-OLD")             # alt
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb',
                        lambda h: {"row": ap, "fetched_at": 2000.0})

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)

    assert source == "fr24"
    assert obs_ts == 3000.0
    assert row[2] == "LX-FRESH"


def test_tie_breaks_to_higher_rank_fr24(monkeypatch):
    # Gleichstand beim obs_ts → niedrigerer Rang (fr24=1) gewinnt.
    fr = _row(obs_ts=2500.0, reg="LX-TIE")
    ap = _row(obs_ts=2500.0, reg="D-TIE")
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb',
                        lambda h: {"row": ap, "fetched_at": 2500.0})

    row, source, obs_ts, tried = WR.position_for_flight(hex="4d0113")
    assert source == "fr24"
    assert obs_ts == 2500.0


# ─── (c) Family (allow_paid=False) fasst den bezahlten Tier NIE an ───────────
def test_family_allow_paid_false_never_touches_paid_tier(monkeypatch):
    # Alle Tabellen leer (Default-Fixture). Family-Watch ist targeted, aber
    # allow_paid=False → Tier 4 (AeroDataBox) MUSS geschlossen bleiben.
    # Auch der freie Mirror (Tier 3) läuft — er ist gratis — aber liefert nichts.
    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", reg="D-AIPA", targeted=True, allow_paid=False)

    assert source == "none"
    assert row is None
    assert obs_ts is None
    ADSB._adb_position_attempt.assert_not_called()   # bezahlt: NIE angefasst


def test_family_bulk_not_targeted_touches_nothing_external(monkeypatch):
    # Bulk-Radar (fremde Flieger): targeted=False → weder freier Mirror noch
    # bezahlt. Reiner Tabellen-Read; leer → 'none'.
    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=False, allow_paid=False)

    assert source == "none"
    ADSB._fetch_adsb_lol.assert_not_called()
    ADSB._adb_position_attempt.assert_not_called()


# ─── (d) Kill-Switch-Default: kein Selbst-Harvest, Store-Read funktioniert ────
def test_fr24_selfharvest_off_by_default_store_read_works(monkeypatch):
    # ECHTES _fetch_fr24 (nicht das inerte Fixture-Mock): der In-Memory-Store
    # ist gefüllt (Harvester-Flotte-Simulation), Supabase ist kalt/aus. Ohne den
    # Kill-Switch-Override (FR24_BACKEND_SELFHARVEST != '1') darf das Backend
    # NICHT selbst harvesten — es liest NUR den Store.
    monkeypatch.setattr(ADSB, '_fetch_fr24', _real_fetch_fr24)
    # Supabase aus → _fr24_warm_from_store no-op; Store gilt als kalt.
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (None, False))
    ADSB._FR24["store_at"] = 0.0
    ADSB._FR24["store_fresh_at"] = 0.0

    # Selbst-Harvest scharf beobachten.
    self_harvest = MagicMock()
    monkeypatch.setattr(ADSB, '_fr24_refresh_one_tile', self_harvest)

    # Store IST gefüllt (der verteilte Harvester hat geschrieben).
    fr = _row(obs_ts=time.time())
    ADSB._FR24["entries"]["4d0113"] = (fr, time.time())
    ADSB._FR24["by_cs"]["CLX4327"] = "4d0113"

    got = ADSB._fetch_fr24("4d0113")

    self_harvest.assert_not_called()             # Kill-Switch-Default: KEIN Harvest
    assert got is fr                             # reiner Store-Read liefert die Row


def test_fr24_selfharvest_off_end_to_end_through_resolver(monkeypatch):
    # Dasselbe, aber durch die EINE Kaskade: Store-Read speist Tier 1, ohne dass
    # der User-Request extern geht. aircraft_positions/extern bleiben leer.
    monkeypatch.setattr(ADSB, '_fetch_fr24', _real_fetch_fr24)  # echtes, nicht inert
    monkeypatch.setattr(ADSB, '_sb_client', lambda: (None, False))
    self_harvest = MagicMock()
    monkeypatch.setattr(ADSB, '_fr24_refresh_one_tile', self_harvest)

    fr = _row(obs_ts=time.time())
    ADSB._FR24["entries"]["4d0113"] = (fr, time.time())
    ADSB._FR24["store_at"] = 0.0
    ADSB._FR24["store_fresh_at"] = 0.0

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)

    assert source == "fr24"
    assert row is fr
    self_harvest.assert_not_called()
    ADSB._adb_position_attempt.assert_not_called()


# ─── (e) targeted + alle Tabellen leer → GENAU EIN budget-gedeckelter ADB-Call ─
def test_targeted_all_tables_empty_exactly_one_paid_attempt(monkeypatch):
    # Tabellen leer (Default), freier Mirror leer (Default). targeted + allow_paid
    # → der einzige erlaubte Extern-Zugriff: EIN AeroDataBox-Versuch.
    adb = _row(obs_ts=9000.0, reg="D-AIPA")
    paid = MagicMock(return_value=(adb, 9000.0, None))
    monkeypatch.setattr(ADSB, '_adb_position_attempt', paid)

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", reg="D-AIPA", targeted=True, allow_paid=True)

    assert source == "adb"
    assert obs_ts == 9000.0
    assert row is adb
    paid.assert_called_once()                    # GENAU EIN bezahlter Versuch
    # Der freie Mirror lief davor (gratis), lieferte aber nichts.
    ADSB._fetch_adsb_lol.assert_called_once()


def test_targeted_free_mirror_hit_never_reaches_paid(monkeypatch):
    # Wenn der GRATIS-Mirror (Tier 3) trifft, wird der bezahlte Tier 4 NICHT
    # mehr angefasst — der budget-gedeckelte Notnagel bleibt ungenutzt.
    lol = _row(obs_ts=7000.0, reg="D-FREE")
    monkeypatch.setattr(ADSB, '_fetch_adsb_lol', MagicMock(return_value=lol))
    paid = MagicMock(return_value=(_row(obs_ts=9000.0), 9000.0, None))
    monkeypatch.setattr(ADSB, '_adb_position_attempt', paid)

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", reg="D-FREE", targeted=True, allow_paid=True)

    assert source == "adsb.lol"
    assert obs_ts == 7000.0
    paid.assert_not_called()


# ═══════════════════════════════════════════════════════════════
#  Adversarial-Review-Regressionen (2026-07-06) — Frische-Fälschung
#  & Fallback-Erreichbarkeit dürfen nie wiederkommen.
# ═══════════════════════════════════════════════════════════════

# ─── (1) row[3]=None fabriziert NICHT obs_ts=now und schlägt kein echtes ap ────
def test_fr24_without_ts_does_not_beat_fresher_aircraft_positions(monkeypatch):
    now = time.time()
    # fr24-Row OHNE Beobachtungszeit (row[3]=None) — z.B. Harvester-Row ohne
    # time_position und ohne parsebares updated_at → muss als ÄLTEST gelten.
    fr = _row(obs_ts=None, reg="LX-NOTS")
    assert fr[3] is None
    # aircraft_positions: echt & frisch (30s alt).
    ap = _row(obs_ts=now - 30.0, reg="D-REAL")
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb',
                        lambda h: {"row": ap, "fetched_at": now - 30.0})

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)

    # Die zeitstempel-lose fr24-Row darf die frische echte ap NICHT überranken.
    assert source == "aircraft_positions"
    assert row[2] == "D-REAL"
    assert obs_ts == now - 30.0
    # Und die fr24-Diagnose meldet NIE obs_ts==now (keine „jetzt"-Fälschung).
    fr_diag = [t for t in tried if t.get("upstream") == "fr24"
               and t.get("ok") and "obs_ts" in t]
    assert fr_diag, "fr24 candidate must be diagnosed"
    assert fr_diag[0]["obs_ts"] == 0.0
    assert fr_diag[0]["obs_ts"] != pytest.approx(now, abs=5)


# ─── (2) estimated-fr24 überrankt keinen echten Fix — auch nicht wenn frischer ─
def test_estimated_fr24_does_not_override_real_fix(monkeypatch):
    now = time.time()
    # fr24 estimated (MLAT/extrapoliert, position_source=2) UND frischer.
    fr = _row(obs_ts=now - 5.0, reg="LX-EST")
    fr[16] = 2                                     # OpenSky: 2 = MLAT/estimated
    # aircraft_positions: echter Fix, aber ÄLTER.
    ap = _row(obs_ts=now - 120.0, reg="D-REAL")
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb',
                        lambda h: {"row": ap, "fetched_at": now - 120.0})

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)

    # ECHT schlägt GESCHÄTZT, obwohl der estimated-Fix frischer ist.
    assert source == "aircraft_positions"
    assert row[2] == "D-REAL"
    assert obs_ts == now - 120.0


def test_estimated_fr24_used_only_when_no_real_fix(monkeypatch):
    now = time.time()
    fr = _row(obs_ts=now - 5.0, reg="LX-EST")
    fr[16] = 2
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)
    # aircraft_positions leer → estimated ist der EINZIGE Kandidat → wird genutzt.
    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)
    assert source == "fr24"
    assert row[2] == "LX-EST"


# ─── (3) ms-/Zukunfts-row[3] gewinnt nicht und ist nicht 'confirmed' ──────────
def test_future_or_ms_fr24_ts_not_chosen_over_real(monkeypatch):
    now = time.time()
    # Millisekunden-Zeitstempel (> 1e12) — verdächtig, muss als ältest gelten.
    fr = _row(obs_ts=now * 1000.0, reg="LX-MS")
    ap = _row(obs_ts=now - 40.0, reg="D-REAL")
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)
    monkeypatch.setattr(ADSB, '_backfill_cache_from_sb',
                        lambda h: {"row": ap, "fetched_at": now - 40.0})

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)

    assert source == "aircraft_positions"
    assert obs_ts == now - 40.0


def test_ms_ts_fr24_alone_not_confirmed_and_clamped(monkeypatch):
    now = time.time()
    fr = _row(obs_ts=now * 1000.0, reg="LX-MS")    # ms → implausibel
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)

    row, source, obs_ts, tried = WR.position_for_flight(
        hex="4d0113", targeted=True, allow_paid=True)

    # Einziger Kandidat → wird geliefert, aber der implausible TS ist auf 0
    # geclampt und gilt NIEMALS als „confirmed live".
    assert source == "fr24"
    assert obs_ts == 0.0
    sel = [t for t in tried if t.get("selected") == "fr24"]
    assert sel and sel[0]["confirmed"] is False


def test_future_ts_alone_not_confirmed(monkeypatch):
    now = time.time()
    fr = _row(obs_ts=now + 10000.0, reg="LX-FUT")  # Zukunft → implausibel
    monkeypatch.setattr(ADSB, '_fetch_fr24', lambda h, callsign=None: fr)

    row, source, obs_ts, tried = WR.position_for_flight(hex="4d0113")

    assert source == "fr24"
    assert obs_ts == 0.0                           # auf ältest geclampt
    sel = [t for t in tried if t.get("selected") == "fr24"]
    assert sel and sel[0]["confirmed"] is False


# ─── (4) get_adsb_state Total-Miss erreicht den no_signal-Pfad (source→None) ──
def test_get_adsb_state_total_miss_reaches_no_signal(monkeypatch):
    # Alle Tabellen leer (Default-Fixture), freier Mirror sauberes „kein Signal",
    # AeroDataBox ohne Treffer. Der Resolver liefert 'none' — der Handler MUSS
    # das auf None mappen und die ehrlichen Fallbacks erreichen (statt via
    # „source is not None"-Gate fälschlich eine Live-Position=null zu servieren).
    import app as _app_mod
    monkeypatch.setattr(ADSB, '_rate_limited', lambda **k: False)
    monkeypatch.setattr(ADSB, '_touch_watch', lambda *a, **k: None)
    # adsb.lol: sauberes Miss (ok:True im tried) → no_signal, nicht 502.
    monkeypatch.setattr(ADSB, '_fetch_adsb_lol', MagicMock(return_value=None))

    _app_mod.app.testing = True
    client = _app_mod.app.test_client()
    resp = client.get('/api/adsb/state?hex=4d0113&own=1')

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["position"] is None
    assert body["source"] == "no_signal"
    # Beweis, dass der Miss-Pfad (nicht der Live-Gate) lief: kein cached-Live.
    assert body.get("cached") is False
