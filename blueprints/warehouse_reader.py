# ═══════════════════════════════════════════════════════════════
#  Warehouse-Reader — EINE Positions-Quelle für ALLE „wo ist der Flieger"-Fragen
#
#  Owner-Ziel (2026-07-06): eigene Live-Position, „nächster Flieger",
#  Family-„fliegt gerade" und Freunde/Crew-Radar ziehen ihre Position aus
#  GENAU EINER Kaskade — statt aus vier verschiedenen (mal Live-Ping, mal
#  berechnet, mal bezahlt), die sich widersprechen.
#
#  4-stufige Reihenfolge (PRIMÄR = die vom verteilten Harvester gefüllten
#  Tabellen, extern nur als budget-gedeckelter targeted-Notnagel):
#    1) fr24_live            — Tabelle, vom NAS/VM-Harvester gefüllt (hex, sek.
#                              callsign). Beobachtungszeit = row[3] (time_position).
#    2) aircraft_positions   — Tabelle, vom ADS-B-Poller warm gehalten
#                              (/api/adsb/poll auf das Watch-Set). Beobachtungs-
#                              zeit = row[3] (last_seen_unix).
#    3) freier ADS-B-Mirror  — adsb.lol/fi/airplanes.live (_fetch_adsb_lol).
#                              NUR bei targeted UND Tabellen-Miss.
#    4) AeroDataBox          — _adb_position_attempt. NUR bei targeted (eigen/
#                              watch) + allow_paid + Budget-Gate. LETZTER Notnagel.
#
#  AUSWAHL NACH FRISCHE, NICHT NACH RANG: unter den (immer gelesenen) Tabellen
#  gewinnt die frischeste ECHTE Beobachtung (max obs_ts = row[3]); der Rang
#  bricht nur Gleichstände. Eine geschätzte/interpolierte Position gewinnt NIE
#  über einen echten Fix. WICHTIG: fr24_live wird mit &estimated=1 geholt und
#  kann MLAT-/extrapolierte Rows enthalten (kein direkter ADS-B-Receiver). Solche
#  Rows werden als estimated markiert (OpenSky position_source=2 in row[16]) und
#  dürfen einen echten Fix NIE allein über Frische überranken — echt schlägt
#  geschätzt, erst danach zählt obs_ts. aircraft_positions/adsb.lol/AeroDataBox
#  liefern echte Fixes.
#
#  KERN-REGEL (5000 User): KEIN User-Request fasst extern an, außer dem budget-
#  gedeckelten targeted-Notnagel. Bulk (Family/andere) liest NUR die Tabellen
#  (Tier 1+2). Der Hintergrund-Poller hält aircraft_positions frisch — darauf
#  verlassen, nicht pro Request extern gehen.
#
#  Row-Format überall = OpenSky-State-Array (siehe _fetch_adsb_lol /
#  _normalize_adsb_lol_ac): [0] icao24, [1] callsign, [2] reg,
#  [3] time_position (ECHTE Beobachtungszeit, unix), [4] last_contact, [5] lon,
#  [6] lat, [7] baro_alt_m, [8] on_ground, [9] velocity_m_s, [10] true_track,
#  [11] vertical_rate, [12] sensors, [13] geo_alt_m, [14] squawk, [15] spi,
#  [16] position_source.
#
#  Alle Helfer werden LAZY aus adsb_blueprint importiert, um den Blueprint-
#  Zirkel zu vermeiden (adsb_blueprint._live_position_cascade delegiert
#  umgekehrt hierher).
# ═══════════════════════════════════════════════════════════════

import time

# Ein echter Fix jünger als das gilt als „bestätigt live"; alles darüber wird
# ehrlich mit seinem (älteren) obs_ts zurückgegeben, nie als frisch getarnt.
FRESH_CONFIRM_S = 90.0


def _obs_ts_of(row):
    """ECHTE Beobachtungszeit einer OpenSky-State-Row = row[3] (time_position).
    None wenn nicht bestimmbar."""
    try:
        v = row[3]
        return float(v) if v is not None else None
    except (IndexError, TypeError, ValueError):
        return None


def _clamp_obs_ts(ts, now, epsilon=5.0):
    """Plausibilitäts-Clamp für Beobachtungszeitstempel VOR dem Frische-Vergleich.

    Ein Zukunfts-Zeitstempel (> now+epsilon) oder ein Millisekunden-verdächtiger
    Wert (> 1e12, d.h. jemand hat ms statt s geschrieben) ist keine echte frische
    Beobachtung — er würde sonst jeden max()-Vergleich fälschlich gewinnen und mit
    age_s auf 0 geclampt als „confirmed live" durchgehen. Solche Werte werden als
    ÄLTEST (0.0) behandelt. Nicht-parsebares → 0.0. Rückgabe: bereinigter float."""
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return 0.0
    if t != t:                 # NaN
        return 0.0
    if t > 1e12:               # Millisekunden statt Sekunden → implausibel
        return 0.0
    if t > now + epsilon:      # Zukunft → implausibel
        return 0.0
    if t < 0:
        return 0.0
    return t


def _confirmed(now, obs_ts):
    """„bestätigt live" NUR, wenn die Beobachtung 0..FRESH_CONFIRM_S alt ist.
    Ein Zukunfts-/None-Zeitstempel ist NIE confirmed (verhindert das alte Verhalten,
    bei dem ein Zukunfts-TS via age→0-Clamp fälschlich als frisch galt)."""
    if obs_ts is None:
        return False
    try:
        age = now - float(obs_ts)
    except (TypeError, ValueError):
        return False
    return 0.0 <= age < FRESH_CONFIRM_S


def _is_estimated_row(row):
    """True wenn die Row eine geschätzte/MLAT-Position ist statt eines echten
    ADS-B-Fixes. Trägt sich über OpenSky position_source (row[16]): 0=ADS-B (echt),
    1=ASTERIX (echt), 2=MLAT/estimated, 3=FLARM. None/unbekannt → als ECHT
    behandeln (nichts fälschlich down-ranken; Test-Rows ohne position_source
    bleiben echte Fixe)."""
    try:
        ps = row[16]
    except (IndexError, TypeError):
        return False
    return ps == 2


def position_for_flight(hex=None, reg=None, callsign=None,
                        targeted=False, allow_paid=True):
    """Die EINE Positions-Kaskade für alle „wo ist der Flieger"-Fragen.

    Args:
        hex       — icao24 (bevorzugt; lowercase egal).
        reg       — Registrierung; wird zu hex aufgelöst wenn hex fehlt.
        callsign  — optionaler Callsign-Fallback (fr24 sekundär, adb best-effort).
        targeted  — True nur für gezielte Abfragen (eigener Flug / Inbound /
                    Family-/Freunde-Watch). Schaltet die externen Tiers 3+4 frei.
                    Bulk/Radar (fremde Flieger) MUSS targeted=False → Tabellen-only.
        allow_paid— erlaubt (zusätzlich zu targeted) den bezahlten Tier 4
                    (AeroDataBox). Family ruft mit allow_paid=False.

    Returns (row, source, obs_ts, tried):
        row     — OpenSky-State-Array oder None (nichts gefunden).
        source  — Provenienz: 'fr24' | 'aircraft_positions' | 'adsb.lol' | 'adb'
                  bei Treffer; 'none' wenn nichts gefunden (aber Lookups liefen —
                  non-None, damit bestehende Route-Caller ihren Stale-Fallback
                  fahren können).
        obs_ts  — ECHTER Beobachtungs-Zeitstempel (unix) der zurückgegebenen Row
                  (= row[3]); None wenn kein Treffer. NIE „jetzt" für alte Fixe.
        tried   — Diagnose-Liste; JEDER Eintrag trägt den Schlüssel 'upstream'
                  (öffentlicher API-Contract, von Route-/Family-Consumern gelesen):
                  pro Tier {upstream, ok, reason|obs_ts} + finaler Treffer-Eintrag
                  {upstream, selected, obs_ts, age_s, estimated, confirmed}. Ein
                  sauberer Tabellen-/Mirror-Miss trägt ok:True (Store gelesen, kein
                  Treffer) — NICHT als Fehler, damit der no_signal-vs-502-Gate im
                  Handler einen leeren Store nicht als „alle Upstreams tot" wertet.
    """
    # Lazy-Import gegen den Blueprint-Zirkel (adsb_blueprint delegiert hierher).
    from blueprints import adsb_blueprint as A

    tried = []
    now = time.time()

    hex_l = (hex or '').strip().lower()
    reg_u = (reg or '').strip().upper()
    cs = (callsign or '').strip().upper() or None

    # Reg → Hex auflösen, wenn nur die Registrierung gegeben ist (Tabellen sind
    # hex-gekeyt). Best-effort; Miss ist ok (fr24 kann per Callsign matchen).
    if not hex_l and reg_u:
        try:
            hex_l = (A.resolve_reg_to_hex(reg_u) or '').strip().lower()
        except Exception:
            hex_l = ''

    # (obs_ts, rank, row, source, is_estimated) — is_estimated hat Vorrang vor
    # obs_ts (echt schlägt geschätzt), Rang bricht nur echten Gleichstand.
    candidates = []

    # ─── Tier 1: fr24_live (Tabelle, PRIMÄR für alles) ───
    # _fetch_fr24 liest nach dem Kill-Switch (FR24_BACKEND_SELFHARVEST) NUR den
    # vom Harvester gefüllten Store — kein Selbst-Harvest im User-Pfad.
    if hex_l or cs:
        try:
            fr = A._fetch_fr24(hex_l or None, cs)
        except Exception as e:
            tried.append({"upstream": "fr24", "ok": False,
                          "reason": str(e)[:80]})
        else:
            if fr is not None:
                # ECHTE Beobachtungszeit = row[3]. Fehlt sie, hat _fr24_warm_from_store
                # bereits den WAHREN Store-Zeitstempel (fr24_live.updated_at) in row[3]
                # gelegt; ist auch der None → als ÄLTEST behandeln (obs_ts=0), NIE „jetzt"
                # fabrizieren (sonst schlägt eine zeitstempel-lose Row eine echte
                # aircraft_positions). Plausibilitäts-Clamp gegen Zukunft/Millisekunden.
                ts = _obs_ts_of(fr)
                ts = _clamp_obs_ts(ts if ts is not None else 0.0, now)
                est = _is_estimated_row(fr)
                candidates.append((ts, 1, fr, "fr24", est))
                tried.append({"upstream": "fr24", "ok": True, "obs_ts": ts,
                              "estimated": est})
            else:
                # ok:True = Tabelle SAUBER gelesen, nur kein Treffer (kein Fehler).
                # Der no_signal-vs-502-Gate im Handler braucht das, um einen leeren
                # Store nicht als „alle Upstreams tot" (502) zu missdeuten.
                tried.append({"upstream": "fr24", "ok": True,
                              "reason": "miss"})

    # ─── Tier 2: aircraft_positions (Tabelle, vom ADS-B-Poller warm) ───
    if hex_l:
        try:
            bf = A._backfill_cache_from_sb(hex_l)
        except Exception as e:
            bf = None
            tried.append({"upstream": "aircraft_positions", "ok": False,
                          "reason": str(e)[:80]})
        if bf is not None and bf.get("row") is not None:
            ap_row = bf["row"]
            # row[3] = last_seen_unix (ECHT). Fehlt sie, den WAHREN Record-Zeitstempel
            # (aircraft_positions.fetched_at, von _backfill_cache_from_sb als
            # bf['fetched_at'] geliefert) nehmen — NIE „jetzt". Fehlt auch der → ältest.
            ts = _obs_ts_of(ap_row)
            if ts is None:
                try:
                    ts = float(bf.get("fetched_at"))
                except (TypeError, ValueError):
                    ts = None
                if ts is None:
                    ts = 0.0
            ts = _clamp_obs_ts(ts, now)
            candidates.append((ts, 2, ap_row, "aircraft_positions", False))
            tried.append({"upstream": "aircraft_positions", "ok": True,
                          "obs_ts": ts})
        elif hex_l:
            tried.append({"upstream": "aircraft_positions", "ok": True,
                          "reason": "miss"})

    # ─── Auswahl NACH FRISCHE unter den Tabellen-Treffern ───
    # Reihenfolge der Kriterien: (1) ECHT vor geschätzt (ein estimated-Kandidat
    # überrankt NIE einen echten Fix, egal wie frisch); (2) max echtem obs_ts;
    # (3) Gleichstand → niedrigerer Rang (fr24 vor aircraft_positions). Extern
    # (Tier 3/4) NUR bei TABELLEN-MISS.
    if candidates:
        def _selkey(c):
            obs_ts_c, rank_c, _row_c, _src_c, est_c = c
            return (0 if est_c else 1, obs_ts_c, -rank_c)
        best = max(candidates, key=_selkey)
        obs_ts, _rank, row, source, _est = best
        tried.append({"upstream": source, "selected": source, "obs_ts": obs_ts,
                      "age_s": int(max(0.0, now - obs_ts)),
                      "estimated": _est,
                      "confirmed": _confirmed(now, obs_ts)})
        return row, source, obs_ts, tried

    # ─── Tier 3: freier ADS-B-Mirror — NUR targeted & Tabellen-Miss ───
    if targeted and hex_l:
        try:
            lol = A._fetch_adsb_lol(hex_l)
        except A._UpstreamError as e:
            tried.append({"upstream": "adsb.lol", "ok": False,
                          "reason": str(e)[:80]})
        else:
            if lol is not None:
                obs_ts = _obs_ts_of(lol)
                obs_ts = _clamp_obs_ts(obs_ts if obs_ts is not None else 0.0, now)
                tried.append({"upstream": "adsb.lol", "selected": "adsb.lol",
                              "obs_ts": obs_ts,
                              "age_s": int(max(0.0, now - obs_ts)),
                              "confirmed": _confirmed(now, obs_ts)})
                return lol, "adsb.lol", obs_ts, tried
            tried.append({"upstream": "adsb.lol", "ok": True, "reason": "miss"})

    # ─── Tier 4: AeroDataBox (BEZAHLT) — NUR targeted + allow_paid + Budget ───
    if targeted and allow_paid and (hex_l or reg_u):
        adb_row, adb_ts, adb_skip = A._adb_position_attempt(hex_l, reg_u)
        if adb_row is not None:
            obs_ts = adb_ts if adb_ts is not None else _obs_ts_of(adb_row)
            obs_ts = _clamp_obs_ts(obs_ts if obs_ts is not None else 0.0, now)
            tried.append({"upstream": "aerodatabox", "selected": "adb",
                          "obs_ts": obs_ts,
                          "age_s": int(max(0.0, now - obs_ts)),
                          "confirmed": _confirmed(now, obs_ts)})
            return adb_row, "adb", obs_ts, tried
        tried.append({"upstream": "aerodatabox", "ok": False, "reason": adb_skip})

    # ─── Nichts gefunden ───
    # source='none' (non-None) statt None: die bestehende Route-Logik unterscheidet
    # „Quelle erreicht, kein Signal" (→ ehrlicher Stale-Fallback) von „gar nichts
    # versucht". Kein Treffer → kein obs_ts (nie „jetzt" erfinden).
    return None, "none", None, tried
