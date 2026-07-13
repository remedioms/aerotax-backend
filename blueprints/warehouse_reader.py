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


# ═══════════════════════════════════════════════════════════════
#  Increment 2 — EINE Quelle für ROUTE und STATUS eines Fluges (LH506-Frage)
#
#  Owner-Ziel (2026-07-06): „Wenn ich LH506 anschaue — woher kommen die Daten?
#  Alles muss aus EINER Quelle, konsistent." Route/Status/Suche zogen bisher aus
#  verschiedenen Quellen, teils BEZAHLT pro User-Request (AeroDataBox/Aviation-
#  Stack) → teuer + widersprüchlich. NEU: free-first aus UNSEREN Tabellen; bezahlt
#  nur als letzter, budget-gedeckelter Notnagel.
#
#  KERN-REGEL: kein User-Request fasst extern/bezahlt an, außer dem budget-
#  gedeckelten Notnagel in route_for_flight (allow_paid). status_for_flight ist im
#  Default STRUKTURELL spend-frei (free_only-Merge). Hintergrund-Poller füllen die
#  Tabellen; wir lesen sie.
#
#  Alle Helfer werden LAZY importiert (aerox_data_blueprint = D, app = _life_app),
#  um Blueprint-Zirkel/Import-Reihenfolge-Fallen zu vermeiden.
# ═══════════════════════════════════════════════════════════════


def _leg_time_epoch(val, iata):
    """Station-lokalen Route-Zeit-String (`sched_dep`/`est_arr` …) → Unix-Epoche
    (UTC). None wenn leer ODER nicht verlässlich absolut bestimmbar. Nutzt lazy
    app._board_local_to_utc_iso (dort lebt das Stations-TZ-Wissen); ein naiver
    String OHNE ableitbare TZ bleibt None (nie geraten)."""
    if not val:
        return None
    from datetime import datetime
    try:
        from blueprints.aerox_data_blueprint import _life_app
        conv = _life_app('_board_local_to_utc_iso')
    except Exception:
        conv = None
    iso = None
    if conv is not None:
        try:
            iso = conv(val, iata)
        except Exception:
            iso = None
    s = iso or str(val)
    try:
        dt = datetime.fromisoformat(s.strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        return None                 # naiv + keine TZ-Konversion → nicht absolut
    return dt.timestamp()


def _leg_window_allows(route, date=None, arr_epoch=None):
    """SUCHE-Gate (keine Live-lat/lon): akzeptiere eine Cache/Route-Row NUR, wenn
    ihr dep/arr-Zeitfenster den JETZT-Zeitpunkt (großzügig gepolstert) einschließt
    — statt des Positions-Gates, das ohne Live-Position nicht greifen kann. Fehlen
    die Zeiten (fr24/generisch tragen keine), kann NICHTS widerlegt werden → im
    Zweifel ZEIGEN (gleiche Ehrlichkeits-Regel wie das Geometrie-Gate). Verwirft
    nur den KLAREN Widerspruch: Leg noch weit in der Zukunft ODER längst gelandet.

    `arr_epoch` (optional, Unix-Sek.): eine EXTERN nachgezogene Ankunftszeit (aus
    _flight_obs_merged), wenn die Route selbst KEINE arr trägt — genau der Fall bei
    den freien Quellen (fr24/Board-Route tragen oft nur est_dep). So kann ein
    bereits gelandetes voriges Leg (arr < now) auch dann verworfen werden, wenn die
    Route-Row keine eigene Ankunftszeit hat."""
    now = time.time()
    src = route.get('src') or route.get('src_icao') or ''
    dst = route.get('dst') or route.get('dst_icao') or ''
    dep = _leg_time_epoch(route.get('est_dep') or route.get('sched_dep'), src)
    arr = _leg_time_epoch(route.get('est_arr') or route.get('sched_arr'), dst)
    if arr is None and arr_epoch is not None:
        arr = arr_epoch             # Board-Ankunft nachgezogen (freie Shape ohne arr)
    PAD = 3 * 3600.0
    if dep is not None and now < dep - PAD:
        return False                # Abflug klar in der Zukunft → nicht „jetzt"
    if arr is not None and now > arr + PAD:
        return False                # längst gelandet → veraltetes Leg
    if dep is not None and arr is None and now > dep + 18 * 3600.0:
        return False                # kein arr bekannt, Abflug > 18 h her → veraltet
    return True                     # im Zweifel: zeigen, nicht verstecken


def _board_arr_epoch(cs, route, date, D):
    """SUCHE-Hilfe: zieht die ECHTE Ankunftszeit eines Legs aus _flight_obs_merged
    (free_only) nach, wenn die aufgelöste Route selbst keine arr trägt (freie
    fr24/Board-Route-Shape = oft nur est_dep). Board-Keys sind IATA-Flugnr + dep/
    arr-IATA; die nehmen wir aus dem Route-Kandidaten. Rückgabe: Unix-Epoche der
    (esti_arr bevorzugt, sonst sched_arr) ODER None (nichts nachziehbar → Gate
    bleibt im Zweifel großzügig)."""
    dst = route.get('dst') or route.get('dst_icao') or ''
    src = route.get('src') or route.get('src_icao') or ''
    try:
        from blueprints.aerox_data_blueprint import _life_app
        merged_fn = _life_app('_flight_obs_merged')
    except Exception:
        merged_fn = None
    if merged_fn is None:
        return None
    try:
        fn = D._callsign_to_iata_flightno(cs) or cs
    except Exception:
        fn = cs
    if not fn:
        return None
    try:
        m = merged_fn(fn, date=date, dep_iata=(src or None),
                      arr_iata=(dst or None), free_only=True)
    except Exception:
        m = None
    if not m:
        return None
    return _leg_time_epoch(m.get('esti_arr') or m.get('sched_arr'),
                           dst or (m.get('arr_iata') or ''))


def route_for_flight(callsign=None, hex=None, reg=None, lat=None, lon=None,
                     track=None, gs=None, on_ground=False, for_search=False,
                     allow_paid=False, date=None):   # Default FALSE (Ultraplan-Fix #3:
                     # war True = Footgun; alle 3 Aufrufer setzen es eh explizit,
                     # aber ein neuer Aufrufer soll NICHT versehentlich Paid ziehen)
    """Die EINE Route-Kaskade (Callsign/Flug → Start/Ziel), free-first, konsistent.

    REIHENFOLGE (frei VOR bezahlt):
      1) ax_route_cache  — date-gekeyt `CS@YYYYMMDD`, dann `REG:<reg>@YYYYMMDD`.
                           Der NACKTE-CS-Key wird NIE genutzt (mehrdeutig über
                           Tage/Legs).
      2) airport_delay_obs / eigene Airport-Tafel (_route_from_obs) — beobachtet.
      3) flights / Flight-Warehouse (_route_from_warehouse) — board-verifiziert.
      4) fr24_live (_route_from_fr24) — gratis verteilter Harvester-Store.
      5) AeroDataBox/AviationStack — BEZAHLT, nur `allow_paid` UND Budget, zuletzt.

    BEWUSST NICHT in der Kaskade: die freie Generik (adsbdb/adsb.lol/hexdb via
    D._free_generic_route). Der Owner hat sie am 2026-07-03 deaktiviert
    („adsbdb/adsb.lol/hexdb ist eh immer falsch") — statische, richtungs-
    unsichere Tabellen liefern regelmäßig das FALSCHE Leg. Nicht wiederbeleben.

    Gate je Kandidat:
      • for_search=False (Live-Tap, Live-lat/lon vorhanden): Positions-/Geometrie-
        Gate (_geometry_allows_route) — verwirft nur den klaren Widerspruch.
      • for_search=True  (Suche, KEINE Live-Position): LEG-ZEITFENSTER-Gate
        (_leg_window_allows) statt Positions-Gate.

    Rückgabe: route-Dict (src/dst[+_icao], source, confidence, ggf. gate/terminal/
    status/reg/sched_*/est_*) oder None. EIN 'confidence'-Feld: 'confirmed' nur bei
    beobachteter/autoritativer Quelle (Cache-confirmed / Tafel / Warehouse / bezahlt
    autoritativ), 'estimated' bei generischem/fr24-Kandidat. Jeder externe/aufgelöste
    Treffer wird via _record_resolved_route in die eigene Warehouse geschrieben."""
    from blueprints import aerox_data_blueprint as D

    cs = (callsign or '').strip().upper().replace(' ', '')
    if not cs:
        return None
    reg_u = (reg or '').strip().upper() or None
    hex_l = (hex or '').strip().lower() or None
    date = date or D._today_utc()
    dk = date.replace('-', '')

    def _accept(route):
        """for_search → Leg-Zeitfenster-Gate; sonst → Positions/Geometrie-Gate.
        Im Suchpfad wird — wenn die Route selbst keine Ankunftszeit trägt (freie
        Shape) — die echte Board-Ankunft aus _flight_obs_merged nachgezogen und
        ins Gate gegeben, damit ein bereits gelandetes voriges Leg fällt."""
        if for_search:
            arr_epoch = None
            if not (route.get('est_arr') or route.get('sched_arr')):
                arr_epoch = _board_arr_epoch(cs, route, date, D)
            return _leg_window_allows(route, date, arr_epoch=arr_epoch)
        return D._geometry_allows_route(route, lat, lon, track, gs, on_ground)

    # ── 0) aircraft_live (NAS-gRPC-Harvester, ~800 aktive Flieger, 60s frisch,
    #    echter Funkname + origin/dest) — für den AKTIVEN Flug die FRISCHESTE
    #    Identitäts+Route-Wahrheit. Owner 2026-07-09: LH1412 fliegt als „DLH8UA",
    #    der Radar-Tap zeigte FRA→SPU statt FRA→BEG, WEIL aircraft_live gar nicht in
    #    der Kaskade war (Ultraplan Phase 1). Callsign-Match (der Blip trägt den
    #    echten Funknamen); nur mit Geometrie-Gate akzeptiert wie jede Live-Quelle.
    try:
        alf = D._aircraft_live_flight(callsign=cs)
    except Exception:
        alf = None
    if alf and alf.get('dep_iata') and alf.get('arr_iata'):
        # flight_no additiv (Owner 2026-07-12): der Harvester kennt die ECHTE
        # IATA-Nummer des aktiven Flugs (DLH54N → LH1138) — der Callout-Header
        # zeigt sie groß statt nur des Funknamens. Beobachtet, nie abgeleitet.
        _al_route = {'src': alf['dep_iata'], 'dst': alf['arr_iata'],
                     'source': 'aircraft_live', 'confidence': 'confirmed',
                     'reg': alf.get('reg'), 'flight_no': alf.get('flight')}
        if _accept(_al_route):
            D._record_resolved_route(cs, reg_u, _al_route, date)
            return _al_route

    # ── 1) Eigene Warehouse: date-gekeyter Cache (nackter-CS-Key NIE) ──────────
    cached = D._cache_get('ax_route_cache', 'flight', f'{cs}@{dk}')
    if cached and (cached.get('src') or cached.get('src_icao')):
        cached.setdefault('confidence', 'confirmed')
        cached['_from'] = 'cache_date'
        if _accept(cached):
            return cached
    if reg_u:
        rc = D._cache_get('ax_route_cache', 'flight', f'REG:{reg_u}@{dk}')
        if rc and (rc.get('src') or rc.get('src_icao')):
            rc.setdefault('confidence', 'confirmed')
            rc['_from'] = 'cache_reg'
            if _accept(rc):
                return rc

    # ── 2) Eigene Airport-Tafel (airport_delay_obs) — beobachtet, autoritativ ──
    try:
        obs = D._route_from_obs(cs)
    except Exception:
        obs = None
    if obs and (obs.get('src') or obs.get('dst')):
        obs['source'] = 'aerox_board'
        obs['confidence'] = 'confirmed'
        if _accept(obs):
            D._record_resolved_route(cs, reg_u, obs, date)
            return obs

    # ── 3) Flight-Warehouse (flights): Board-Tail↔Hex-Match — board-verifiziert ─
    wh = D._route_from_warehouse(hex_l, reg_u)
    if wh and (wh.get('src') or wh.get('dst')):
        wh['confidence'] = 'confirmed'
        if _accept(wh):
            D._record_resolved_route(cs, reg_u, wh, date)
            return wh

    # ── 3.5) FR24 gRPC — per-flight Route-Autorität (nur Live-Pfad) ────────────
    #     Verifiziert 2026-07-07: die Route kommt aus live_feed.extra_info.route DES
    #     AKTUELLEN Flugs (per-flight), nicht aus einer statischen Callsign->Route-
    #     Tabelle → 10/10 korrekt inkl. Ozean + reused-Callsigns (adsbdb 37,5%
    #     falsch, bleibt tot). Anonym/gratis über gRPC (AWS-ELB, kein CF-Block).
    #     Nur wenn eine Live-Position vorliegt (kein Suchpfad, braucht die BBox).
    if not for_search and lat is not None and lon is not None:
        try:
            from blueprints import fr24_grpc
            g = fr24_grpc.resolve_route_live(callsign=cs, hex=hex_l, reg=reg_u,
                                             lat=lat, lon=lon)
        except Exception:
            g = None
        if g and (g.get('src') or g.get('dst')):
            g['source'] = 'fr24_grpc'
            g['confidence'] = 'confirmed'
            if _accept(g):
                D._record_resolved_route(cs, g.get('reg') or reg_u, g, date)
                return g

    # ── 4) fr24_live (GRATIS, verteilter Harvester-Store) — estimated ──────────
    fr = D._route_from_fr24(cs, hex_l)
    if fr and (fr.get('src') or fr.get('dst')):
        fr.setdefault('confidence', 'estimated')
        if _accept(fr):
            D._record_resolved_route(cs, reg_u, fr, date)
            return fr

    # ── (5 übersprungen) Freie Generik (adsbdb/adsb.lol/hexdb) BEWUSST DEAKTIVIERT.
    #     Owner-Entscheid 2026-07-03: statisch/richtungs-unsicher → liefert das
    #     falsche Leg. NICHT wiederbeleben (siehe Docstring). ─────────────────────

    # ── 5) BEZAHLT (LETZTER Notnagel) — nur allow_paid UND Tages-Budget ────────
    if allow_paid and D._paid_budget_ok():
        try:
            adb = D._aerodatabox_route(cs, reg=reg_u, lat=lat, lon=lon,
                                       track=track, date=date)
        except Exception:
            adb = None
        if adb and (adb.get('src') or adb.get('dst')):
            adb['confidence'] = 'confirmed'
            if _accept(adb):
                D._record_resolved_route(cs, adb.get('reg') or reg_u, adb, date)
                return adb
        try:
            avs = D._aviationstack_route(cs)
        except Exception:
            avs = None
        if avs and (avs.get('src') or avs.get('dst')):
            avs['confidence'] = 'confirmed'
            if _accept(avs):
                D._record_resolved_route(cs, reg_u, avs, date)
                return avs

    return None


# ─── STATUS: tokenisierter, seiten-bewusster Board-Status ──────────────────────
# TOKENISIERT statt Substring (Owner-Regel): „at gate" am ORIGIN ≠ gelandet.
# Board-Provider liefern DE+EN gemischt („Gelandet 14:23" / „Landed" / „At Gate").
_STATUS_LANDED = {'landed', 'arrived', 'gelandet', 'angekommen', 'baggage',
                  'gepaeck', 'gepack'}
_STATUS_AIRBORNE = {'departed', 'airborne', 'enroute', 'abgeflogen', 'gestartet',
                    'unterwegs'}
_STATUS_GROUNDED = {'scheduled', 'boarding', 'delayed', 'estimated', 'erwartet',
                    'planmaessig', 'planmassig', 'verspaetet', 'verspatet',
                    'expected', 'calling', 'wait', 'warten'}
_STATUS_CANCELLED = {'cancelled', 'canceled', 'annulliert', 'gestrichen',
                     'storniert'}


def _tokenize_status(status):
    """Board-Status-String → Liste kleingeschriebener Wort-Tokens (Umlaute
    entfaltet, Nicht-Alphanumerik als Trenner, Zeit-Suffixe fallen als eigene
    numerische Tokens weg). Leere Liste bei None/leer."""
    import re as _re
    s = (status or '').strip().lower()
    if not s:
        return []
    s = (s.replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue')
         .replace('ß', 'ss'))
    return [t for t in _re.split(r'[^a-z0-9]+', s) if t and not t.isdigit()]


def _status_phase_of(status, side):
    """EINE Board-Status-Zeile → 'landed'|'airborne'|'grounded'|'cancelled'|None,
    TOKENISIERT und SEITEN-BEWUSST (side ∈ 'dep'|'arr'). None = kein/unbekanntes
    Signal (Aufrufer fällt auf ADS-B/on_ground zurück).

    Kern: 'at gate'/'on ground'/'on blocks' am ZIEL (side='arr') = gelandet; am
    START (side='dep') = am Boden wartend (grounded), NICHT gelandet."""
    toks = _tokenize_status(status)
    if not toks:
        return None
    ts = set(toks)

    def phrase(a, b):
        return any(toks[i] == a and toks[i + 1] == b
                   for i in range(len(toks) - 1))

    if ts & _STATUS_CANCELLED:
        return 'cancelled'
    if ts & _STATUS_LANDED or phrase('on', 'blocks') or phrase('on', 'block'):
        return 'landed'
    # Seiten-abhängig: 'at gate' / 'on ground'
    if phrase('at', 'gate') or phrase('on', 'ground') or phrase('on', 'stand'):
        return 'landed' if side == 'arr' else 'grounded'
    if (ts & _STATUS_AIRBORNE or phrase('en', 'route') or phrase('in', 'air')
            or phrase('im', 'flug')):
        return 'airborne'
    if (ts & _STATUS_GROUNDED or phrase('gate', 'open') or phrase('final', 'call')
            or phrase('go', 'gate')):
        return 'grounded'
    return None


def _status_is_hard(status):
    """True wenn der Board-Status eine HARTE, autoritative Beobachtung trägt
    (gelandet/abgeflogen/annulliert ODER on-ground/at-gate/on-blocks/en-route) —
    im Gegensatz zu WEICHEN pre-departure-Schätzungen (scheduled/estimated/
    boarding/delayed …). Owner-Regel: nur harte Stati dürfen eine FRISCHE ADS-B-
    airborne-Beobachtung überstimmen; ein STALE nur-scheduled/estimated/boarding
    Board-Status darf einen frischen airborne-Fix NICHT auf grounded zurückwerfen.
    None/leer → False (kein Signal, nicht hart)."""
    toks = _tokenize_status(status)
    if not toks:
        return False
    ts = set(toks)

    def phrase(a, b):
        return any(toks[i] == a and toks[i + 1] == b
                   for i in range(len(toks) - 1))

    if ts & (_STATUS_LANDED | _STATUS_AIRBORNE | _STATUS_CANCELLED):
        return True
    return (phrase('on', 'blocks') or phrase('on', 'block')
            or phrase('at', 'gate') or phrase('on', 'ground')
            or phrase('on', 'stand') or phrase('en', 'route')
            or phrase('in', 'air') or phrase('im', 'flug'))


def status_for_flight(callsign=None, reg=None, date=None, origin=None, dest=None,
                      on_ground=None, lat=None, lon=None, allow_paid=False,
                      gs=None, alt=None, track=None, vertical_rate=None,
                      seen_ts=None):
    """Die EINE Status-Kaskade eines Fluges — konsistent mit route_for_flight.

    REIHENFOLGE (Board autoritativ, ADS-B nur zur Phasen-Ergänzung, Delay aus dem
    Dual-Side-Merge):
      1) BOARD (airport_delay_obs + freie Live-Boards via _flight_obs_merged,
         free_only) — autoritativ für die Phase: departed/landed/at-gate/estimated,
         TOKENISIERT + Origin/Dest-bewusst (_status_phase_of). Board-'airborne'
         zeigt auch OHNE Position.
      2) FR24/ADS-B on_ground + Origin/Dest-Kontext — nur wenn das Board KEINE
         Phase liefert: on_ground am ZIEL = gelandet; am ORIGIN vor Abflug =
         taxi-out (grounded), NICHT gelandet; nicht-on_ground = airborne.
      3) DELAY immer aus _flight_obs_merged (EINZIGE Delay-Wahrheit; delay_known:
         unbekannt ≠ pünktlich).

    KEIN Paid im Default-Pfad (allow_paid=False → free_only-Merge). allow_paid=True
    erlaubt zusätzlich die bezahlten Board-Zweige des Merges.

    Rückgabe (dict): phase ∈ {'airborne','landed','grounded','cancelled','unknown'},
    delay_min, delay_known, gate, terminal, sched, est, act, source
    ('board'|'adsb'|'engine'|'none'). Zeiten wie vom Board (station-lokal, wie sonst
    auch); fehlt ein Feld → None (nie erfunden).

    ── FLIGHTSTATE-ENGINE (Layer-3-Flip, Kill-Switch FLIGHTSTATE_LIVE_CALLSIGN=1) ──
    Ist der Kill-Switch gesetzt, übernimmt die PHASEN-AUTORITÄT die EINE Engine
    (blueprints.flight_state.resolve_flight_state) — DIESE Fläche zeigt dann
    DIESELBE Wahrheit wie crew_state/flights_live. status_for_flight ist damit ein
    reiner Board-/ADS-B-COLLECTOR (DESIGN §5): die tokenisierte, seiten-bewusste
    Board-Klassifikation + der schon geladene Positions-Fix werden zu Observations
    geshaped, die Engine reduziert sie mit Airborne-Gate (rohes on_ground wird
    ignoriert — FR24/adsb lügen bei Pushback), Landung-Plausi/Monotonie und
    Sticky-Airborne. So kann ein Pushback-Flieger nie mehr 'airborne' zeigen und ein
    stales Board un-landet keinen frischen Fix. KEIN neuer I/O — die Observations
    kommen ausschliesslich aus dem bereits gemergten `m` + dem übergebenen Fix
    (gs/alt/track/on_ground/seen_ts). Wirft die Engine, bleibt die (unveränderte)
    Legacy-Phase stehen (Fallback = nie schlechter als vorher).

    Zusätzlich (immer, auch ohne Flag): das Engine-PLAUSI-Gate `_landing_is_plausible`
    verwirft eine physisch unmögliche/vor dem Abflug liegende 'landed'-Aussage.

    gs/alt/track/vertical_rate/seen_ts (alle optional, additiv) speisen den
    Positions-Fix der Engine — ohne sie bleibt der Call abwärtskompatibel."""
    out = _legacy_status_for_flight(
        callsign=callsign, reg=reg, date=date, origin=origin, dest=dest,
        on_ground=on_ground, lat=lat, lon=lon, allow_paid=allow_paid)
    _apply_flightstate_engine(
        out, callsign=callsign, reg=reg, date=date, origin=origin, dest=dest,
        on_ground=on_ground, lat=lat, lon=lon, allow_paid=allow_paid,
        gs=gs, alt=alt, track=track, vertical_rate=vertical_rate, seen_ts=seen_ts)
    return out


def _legacy_status_for_flight(callsign=None, reg=None, date=None, origin=None,
                              dest=None, on_ground=None, lat=None, lon=None,
                              allow_paid=False):
    """Die Board-/ADS-B-Kaskade (unverändertes Alt-Verhalten). Wird von
    status_for_flight aufgerufen; die Phasen-Autorität kann die Engine übernehmen
    (siehe status_for_flight-Docstring)."""
    from blueprints import aerox_data_blueprint as D

    cs = (callsign or '').strip().upper().replace(' ', '')
    reg_u = (reg or '').strip().upper() or None
    date = (date or '').strip()[:10] or None
    origin = (origin or '').strip().upper() or None
    dest = (dest or '').strip().upper() or None

    out = {'phase': 'unknown', 'delay_min': None, 'delay_known': False,
           'gate': None, 'terminal': None, 'sched': None, 'est': None,
           'act': None, 'source': 'none'}

    # ── Flugnummer für den Dual-Side-Merge (Board-Keys sind IATA-Flugnr) ──
    fn = None
    if cs:
        try:
            fn = D._callsign_to_iata_flightno(cs) or cs
        except Exception:
            fn = cs

    m = None
    if fn:
        merged_fn = None
        try:
            from blueprints.aerox_data_blueprint import _life_app
            merged_fn = _life_app('_flight_obs_merged')
        except Exception:
            merged_fn = None
        if merged_fn is not None:
            try:
                # free_only = KEIN Paid; allow_paid=True hebt das Board-Spend-Gate.
                m = merged_fn(fn, date=date, dep_iata=origin, arr_iata=dest,
                              free_only=(not allow_paid))
            except Exception:
                m = None

    # ── 3) DELAY (immer aus dem Merge — einzige Delay-Wahrheit) ──
    if m is not None:
        out['delay_known'] = bool(m.get('delay_known'))
        out['delay_min'] = m.get('delay_min') if out['delay_known'] else None
        # Origin/Dest ggf. aus dem Merge nachziehen (für den on_ground-Kontext).
        origin = origin or ((m.get('dep_iata') or '').upper() or None)
        dest = dest or ((m.get('arr_iata') or '').upper() or None)

    # Merged-Record + aufgelöste Strecke fürs Engine-Reuse durchreichen (KEIN
    # zweiter Merge-Call). Private Keys — der Engine-Hook liest & entfernt sie.
    out['_m'] = m
    out['_origin'] = origin
    out['_dest'] = dest
    out['_fn'] = fn

    # ── 1) BOARD autoritativ: tokenisierte, seiten-bewusste Phase ──
    board_phase = None
    if m is not None:
        p_arr = _status_phase_of(m.get('status_arr'), 'arr')
        p_dep = _status_phase_of(m.get('status_dep'), 'dep')
        if m.get('cancelled') or p_arr == 'cancelled' or p_dep == 'cancelled':
            board_phase = 'cancelled'
        elif p_arr == 'landed' or p_dep == 'landed':
            board_phase = 'landed'          # Ankunft ist definitiv
        elif p_dep == 'airborne' or p_arr == 'airborne':
            board_phase = 'airborne'
        elif p_dep == 'grounded' or p_arr == 'grounded':
            board_phase = 'grounded'
            # Owner-Regel: ein STALE nur-scheduled/estimated/boarding Board-Status
            # darf eine FRISCHE ADS-B-airborne-Beobachtung (on_ground=False) NICHT
            # auf grounded zurückwerfen. Nur wenn KEINE Seite eine harte Boden-/
            # Lande-/Abflug-Beobachtung trägt, weicht der weiche grounded dem
            # frischen airborne-Fix (→ board_phase fällt weg, ADS-B übernimmt unten).
            if (on_ground is False
                    and not _status_is_hard(m.get('status_dep'))
                    and not _status_is_hard(m.get('status_arr'))):
                board_phase = None
        if board_phase is not None:
            out['phase'] = board_phase
            out['source'] = 'board'
            # Gate/Terminal + Zeiten je Phase: gelandet → Ankunftsseite, sonst
            # Abflugseite. Nur echte Board-Werte, fehlt eins → None.
            if board_phase == 'landed':
                out['gate'] = m.get('gate_arr')
                out['terminal'] = m.get('terminal_arr')
                out['sched'] = m.get('sched_arr')
                out['est'] = m.get('esti_arr')
                out['act'] = m.get('esti_arr')   # Ist-Ankunft = beobachtetes esti
            else:
                out['gate'] = m.get('gate_dep')
                out['terminal'] = m.get('terminal_dep')
                out['sched'] = m.get('sched_dep')
                out['est'] = m.get('esti_dep')
                if board_phase == 'airborne':
                    out['act'] = m.get('esti_dep')  # Ist-Abflug
            return out

    # ── 2) FR24/ADS-B on_ground + Origin/Dest-Kontext (Board gab keine Phase) ──
    if on_ground is not None:
        out['source'] = 'adsb'
        if not on_ground:
            out['phase'] = 'airborne'        # in der Luft → airborne (ohne Board)
            return out
        # on_ground: am ZIEL = gelandet; am ORIGIN vor Abflug = taxi-out (grounded).
        at_origin = _near_airport(lat, lon, origin, D)
        at_dest = _near_airport(lat, lon, dest, D)
        if at_dest and not at_origin:
            out['phase'] = 'landed'
        elif at_origin and not at_dest:
            out['phase'] = 'grounded'        # taxi-out / am Gate, NICHT gelandet
        else:
            # Weder eindeutig am Ziel noch am Start (unbekannte Position / gleiche
            # Stadt) → am Boden, aber Phase nicht sicher „gelandet".
            out['phase'] = 'grounded'
        return out

    # Kein Board, kein ADS-B-Signal → ehrlich unbekannt (Delay ggf. aus Merge da).
    return out


# Kanonische Engine-Phase → Legacy-Status-Vokabular DIESER Fläche (identisch zum
# app-weiten Mapping in family_watch: TAXI_OUT/BOARDING/SCHEDULED = am Boden vor
# Abflug = 'grounded'; DIVERTED = irgendwo gelandet = 'landed'). UNKNOWN → 'unknown'.
_ENGINE_PHASE_TO_LEGACY = {
    'CANCELLED': 'cancelled',
    'LANDED': 'landed', 'ARRIVED': 'landed', 'DIVERTED': 'landed',
    'AIRBORNE': 'airborne', 'APPROACH': 'airborne',
    'TAXI_OUT': 'grounded', 'BOARDING': 'grounded', 'SCHEDULED': 'grounded',
    'UNKNOWN': 'unknown',
}


def _landing_is_plausible(out, m):
    """PLAUSI-Gate (immer aktiv, auch ohne Kill-Switch): eine beobachtete 'landed'-
    Aussage darf NICHT vor/gleich dem effektiven Abflug liegen — das wäre eine
    physisch unmögliche Landung (stale/verwechselte Board-Row). Konservativ: nur
    verwerfen, wenn BEIDE Zeiten sauber parsebar sind UND die Ankunft ≤ Abflug liegt;
    im Zweifel (fehlende/unparsebare Zeit) NICHT verwerfen (keine erfundene Wahrheit).
    Rückgabe True = 'landed' bleibt; False = physisch unmöglich, verwerfen."""
    if not m:
        return True
    from blueprints.flight_state_collectors import _iso_or_epoch
    dep_ts = _iso_or_epoch(m.get('esti_dep') or m.get('act_dep')
                           or m.get('sched_dep'))
    arr_ts = _iso_or_epoch(out.get('act') or m.get('esti_arr')
                           or m.get('sched_arr'))
    if dep_ts is None or arr_ts is None:
        return True                       # nicht widerlegbar → zeigen
    return arr_ts > dep_ts


def _apply_flightstate_engine(out, callsign=None, reg=None, date=None, origin=None,
                              dest=None, on_ground=None, lat=None, lon=None,
                              allow_paid=False, gs=None, alt=None, track=None,
                              vertical_rate=None, seen_ts=None):
    """Phasen-Autorität an die EINE FlightState-Engine übergeben (Layer-3-Flip).

    Reuse: der von _legacy_status_for_flight schon geladene Merge-Record + die
    aufgelöste Strecke (private out['_m']/_origin/_dest/_fn) — KEIN neuer Merge-Call.
    Der übergebene Positions-Fix (gs/alt/track/on_ground/seen_ts) wird zur Positions-
    Observation; das Airborne-Gate der Engine ignoriert rohes on_ground.

    Immer (auch ohne Flag): das PLAUSI-Gate verwirft eine physisch unmögliche
    'landed'-Aussage. Nur mit FLIGHTSTATE_LIVE_CALLSIGN=1 übernimmt die Engine die
    volle Phase (Monotonie/Sticky/Airborne-Gate) — sonst bleibt die Legacy-Phase.
    Wirft nie: bei jedem Fehler bleibt out unverändert (Fallback nie schlechter)."""
    m = out.pop('_m', None)
    fn = out.pop('_fn', None)
    origin = out.pop('_origin', None) or origin
    dest = out.pop('_dest', None) or dest

    # ── PLAUSI-Gate (immer): unmögliche 'landed' → auf 'grounded' zurücknehmen. ──
    if out.get('phase') == 'landed' and out.get('source') == 'board':
        try:
            if not _landing_is_plausible(out, m):
                out['phase'] = 'grounded'
                out['act'] = None
        except Exception:
            pass

    import os
    if os.environ.get('FLIGHTSTATE_LIVE_CALLSIGN', '') not in ('1', 'true', 'yes'):
        return
    try:
        from blueprints import aerox_data_blueprint as D
        from blueprints.flight_state import (
            resolve_flight_state, prior_state, remember_state)
        from blueprints.flight_state_collectors import (
            build_keys, obs_from_board_merged, Observation)

        cs = (callsign or '').strip().upper().replace(' ', '') or None
        fn = fn or cs

        def _ll(code):
            try:
                return D._iata_latlon((code or '').upper())
            except Exception:
                return None

        keys = build_keys(
            fn, date, origin, dest, roster_tail=reg, callsign=cs,
            dep_ll=_ll(origin), arr_ll=_ll(dest))

        obs = obs_from_board_merged(m or {}, keys)
        # Positions-Observation aus dem übergebenen Fix (kein Re-Fetch). Nur wenn
        # es überhaupt ein Positions-Signal gibt (Koordinaten ODER on_ground/gs/alt).
        if (lat is not None and lon is not None) or on_ground is not None \
                or gs is not None or alt is not None:
            from blueprints.flight_state_collectors import _obs_ts_or_none
            import time as _t
            _now = _t.time()
            obs.append(Observation('position', {
                'lat': lat, 'lon': lon, 'track': track,
                'gs_kt': gs, 'alt_ft': alt, 'vertical_rate': vertical_rate,
                'on_ground_raw': on_ground, 'position_source': 0,
            }, 'adsb', _obs_ts_or_none(seen_ts, _now)))

        fs = resolve_flight_state(keys, obs, prior=prior_state(fn, date))
        try:
            remember_state(fs)
        except Exception:
            pass
        legacy_phase = _ENGINE_PHASE_TO_LEGACY.get(fs.get('phase'))
        if legacy_phase is not None and legacy_phase != 'unknown':
            out['phase'] = legacy_phase
            out['source'] = 'engine'
        elif legacy_phase == 'unknown' and out.get('phase') == 'unknown':
            # Engine unsicher UND Legacy unsicher → source ehrlich lassen.
            out['source'] = out.get('source') or 'none'
    except Exception:
        # Engine wirft → Legacy-Phase bleibt (Fallback nie schlechter als vorher).
        pass


def _near_airport(lat, lon, iata, D, radius_km=8.0):
    """True wenn (lat,lon) innerhalb radius_km um den Flughafen iata liegt.
    False bei fehlender Position/Airport/Koordinaten (nie geraten)."""
    if lat is None or lon is None or not iata:
        return False
    try:
        ll = D._iata_latlon((iata or '').upper())
    except Exception:
        ll = None
    if not ll:
        return False
    try:
        return D._gc_km(float(lat), float(lon), ll[0], ll[1]) <= radius_km
    except Exception:
        return False
