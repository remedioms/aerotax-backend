"""LH Open API Flight-Facts-Enrichment (Engine A). Rein offline — kein Netz,
kein Key nötig: die Parser/Merge-Logik ist pur, HTTP wird gemockt. Fixture-
Responses sind exakt die verifizierte echte API-Shape (2026-07-21)."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints import lh_open_api as lh


# ── verifizierte echte FlightStatus-Response (Discover 4Y136, FRA→MBA→…) ──────
FS_4Y136 = {"FlightStatusResource": {"Flights": {"Flight": [
    {"Departure": {"AirportCode": "FRA",
                   "ScheduledTimeLocal": {"DateTime": "2026-07-21T19:25"},
                   "ScheduledTimeUTC": {"DateTime": "2026-07-21T17:25Z"},
                   "Terminal": {"Name": "1", "Gate": "C16"}},
     "Arrival": {"AirportCode": "MBA",
                 "ScheduledTimeLocal": {"DateTime": "2026-07-22T05:10"},
                 "ScheduledTimeUTC": {"DateTime": "2026-07-22T02:10Z"},
                 "Terminal": {"Name": "1"}},
     "Equipment": {"AircraftCode": "333", "AircraftRegistration": "DAIKP"},
     "FlightStatus": {"Code": "NA", "Definition": "No status"}},
    {"Departure": {"AirportCode": "MBA",
                   "ScheduledTimeLocal": {"DateTime": "2026-07-22T06:25"},
                   "ScheduledTimeUTC": {"DateTime": "2026-07-22T03:25Z"}},
     "Arrival": {"AirportCode": "JRO",
                 "ScheduledTimeLocal": {"DateTime": "2026-07-22T07:20"},
                 "ScheduledTimeUTC": {"DateTime": "2026-07-22T04:20Z"}},
     "Equipment": {"AircraftCode": "333", "AircraftRegistration": "DAIKP"},
     "FlightStatus": {"Code": "NA", "Definition": "No status"}},
]}}}

# LH400 abgeflogen (Ist-Zeiten + Delay)
FS_LH400 = {"FlightStatusResource": {"Flights": {"Flight":
    {"Departure": {"AirportCode": "FRA",
                   "ScheduledTimeLocal": {"DateTime": "2026-07-21T10:55"},
                   "ScheduledTimeUTC": {"DateTime": "2026-07-21T08:55Z"},
                   "ActualTimeLocal": {"DateTime": "2026-07-21T11:05"},
                   "ActualTimeUTC": {"DateTime": "2026-07-21T09:05Z"},
                   "Terminal": {"Name": "1", "Gate": "Z16"}},
     "Arrival": {"AirportCode": "JFK",
                 "ScheduledTimeLocal": {"DateTime": "2026-07-21T13:35"},
                 "ScheduledTimeUTC": {"DateTime": "2026-07-21T17:35Z"},
                 "EstimatedTimeLocal": {"DateTime": "2026-07-21T13:03"},
                 "EstimatedTimeUTC": {"DateTime": "2026-07-21T17:03Z"},
                 "Terminal": {"Name": "1"}},
     "Equipment": {"AircraftCode": "346", "AircraftRegistration": "DAIHY"},
     "FlightStatus": {"Code": "DP", "Definition": "Flight Departed"}}}}}


def test_is_lh_group():
    assert lh.is_lh_group("4Y136")
    assert lh.is_lh_group("LH 400")
    assert lh.is_lh_group("LX16")
    assert lh.is_lh_group("EW8")
    assert lh.is_lh_group("OS1")
    assert not lh.is_lh_group("AB123")   # nicht Group
    assert not lh.is_lh_group("UA900")   # Partner, aber Budget-Filter aus
    assert not lh.is_lh_group("4Y")      # keine Nummer
    assert not lh.is_lh_group("")


def test_offset_iso():
    assert lh._offset_iso("2026-07-21T10:55", "2026-07-21T08:55Z") == "2026-07-21T10:55:00+02:00"
    assert lh._offset_iso("2026-07-22T05:10", "2026-07-22T02:10Z") == "2026-07-22T05:10:00+03:00"
    # JFK Sommer = UTC-4
    assert lh._offset_iso("2026-07-21T13:35", "2026-07-21T17:35Z") == "2026-07-21T13:35:00-04:00"
    # ohne UTC → naiv, aber :00 aufgefüllt
    assert lh._offset_iso("2026-07-21T13:35", None) == "2026-07-21T13:35:00"
    assert lh._offset_iso(None, None) is None


def test_norm_reg():
    assert lh._norm_reg("DAIKP") == "D-AIKP"
    assert lh._norm_reg("HBJHA") == "HB-JHA"
    assert lh._norm_reg("D-AIKP") == "D-AIKP"     # schon normalisiert
    assert lh._norm_reg("") == ""


def test_leg_to_facts_future():
    facts = lh._leg_to_facts(FS_4Y136["FlightStatusResource"]["Flights"]["Flight"][0])
    assert facts["sched_dep"] == "2026-07-21T19:25:00+02:00"
    assert facts["sched_arr"] == "2026-07-22T05:10:00+03:00"
    assert facts["gate"] == "C16"
    assert facts["terminal"] == "1"
    assert facts["type"] == "333"
    assert facts["reg"] == "D-AIKP"
    assert facts["dep_iata"] == "FRA" and facts["arr_iata"] == "MBA"
    # kein Ist / Delay bei Zukunftsflug
    assert "est_dep" not in facts and "dep_delay_min" not in facts
    # 'No status' wird NICHT als dep_status durchgereicht
    assert "dep_status" not in facts


def test_leg_to_facts_departed_with_delay():
    facts = lh._leg_to_facts(FS_LH400["FlightStatusResource"]["Flights"]["Flight"])
    assert facts["est_dep"] == "2026-07-21T11:05:00+02:00"
    assert facts["dep_delay_min"] == 10
    assert facts["arr_delay_min"] == -32       # verfrüht
    assert facts["reg"] == "D-AIHY"
    assert facts["gate"] == "Z16"
    assert facts["dep_status"] == "Flight Departed"


def test_flight_facts_picks_matching_leg(monkeypatch):
    monkeypatch.setattr(lh, "_KEY", "k"); monkeypatch.setattr(lh, "_SECRET", "s")
    monkeypatch.setattr(lh, "_get", lambda path, caller=None: FS_4Y136)
    # dep/arr wählt das RICHTIGE Leg (FRA-MBA, nicht MBA-JRO)
    f = lh.lh_flight_facts("4Y136", "2026-07-21", "FRA", "MBA")
    assert f["dep_iata"] == "FRA" and f["arr_iata"] == "MBA"
    f2 = lh.lh_flight_facts("4Y136", "2026-07-21", "MBA", "JRO")
    assert f2["dep_iata"] == "MBA" and f2["arr_iata"] == "JRO"


def test_flight_facts_noop_when_unconfigured(monkeypatch):
    monkeypatch.setattr(lh, "_KEY", ""); monkeypatch.setattr(lh, "_SECRET", "")
    assert lh.lh_flight_facts("4Y136", "2026-07-21", "FRA", "MBA") == {}


def test_flight_facts_noop_for_non_group(monkeypatch):
    monkeypatch.setattr(lh, "_KEY", "k"); monkeypatch.setattr(lh, "_SECRET", "s")
    # kein Netz-Call für Nicht-Group-Flug
    called = {"n": 0}
    monkeypatch.setattr(lh, "_get",
                        lambda p, caller=None: called.__setitem__("n", called["n"] + 1) or {})
    assert lh.lh_flight_facts("AB123", "2026-07-21", "X", "Y") == {}
    assert called["n"] == 0


def test_merge_precedence():
    from blueprints.aerox_data_blueprint import _merge_lh_into_facts
    obs = {"sched_dep": "OBS-DEP", "est_dep": "OBS-EST", "dep_delay_min": 0,
           "reg": "D-OLD", "dep_status": "Board-Status", "dep_iata": "FRA"}
    lh_facts = {"sched_dep": "LH-DEP", "gate": "C16", "reg": "D-NEW",
                "est_dep": "LH-EST", "dep_delay_min": 10, "arr_status": "LH-ARR"}
    out = _merge_lh_into_facts(obs, lh_facts)
    # LH autoritativ: sched_dep + reg + gate überschrieben
    assert out["sched_dep"] == "LH-DEP"
    assert out["reg"] == "D-NEW"
    assert out["gate"] == "C16"
    # KONSISTENZ: Ist-Zeit UND Delay zusammen von LH (nicht est=LH, delay=Board)
    assert out["est_dep"] == "LH-EST"
    assert out["dep_delay_min"] == 10
    # Status-Freitext (Board) bleibt, arr_status-Lücke von LH gefüllt
    assert out["dep_status"] == "Board-Status"
    assert out["arr_status"] == "LH-ARR"
    # Routen-Identität bleibt Board (Match-Stabilität)
    assert out["dep_iata"] == "FRA"


def test_merge_empty_lh_is_noop():
    from blueprints.aerox_data_blueprint import _merge_lh_into_facts
    obs = {"sched_dep": "X"}
    assert _merge_lh_into_facts(obs, {}) == obs
    assert _merge_lh_into_facts(obs, None) == obs


def test_merge_stale_obs_yields_pure_lh():
    """Stale (Vortags-)Board wird downstream verworfen — LH ist date-exakt und
    darf NICHT mitverworfen werden: bei stale-Obs pur LH (ohne stale-Flag)."""
    from blueprints.aerox_data_blueprint import _merge_lh_into_facts
    stale_obs = {"sched_dep": "GESTERN", "stale": True, "obs_date": "2026-07-20"}
    lh = {"sched_dep": "HEUTE-LH", "reg": "D-AIKP", "gate": "C16"}
    out = _merge_lh_into_facts(stale_obs, lh)
    assert out == lh                    # pur LH
    assert "stale" not in out           # kein Verwerfen downstream
    # ohne LH bleibt die stale-Obs unverändert (altes Verhalten)
    assert _merge_lh_into_facts(stale_obs, {}) == stale_obs


def test_cached_only_never_blocks_and_warms(monkeypatch):
    # cached_only: Memo-Hit liefert, Miss gibt {} + EIN Hintergrund-Warmup —
    # nie HTTP im Aufrufer-Thread (Incident-Fix 2026-07-22).
    import time as _t
    monkeypatch.setattr(lh, '_KEY', 'k')
    monkeypatch.setattr(lh, '_SECRET', 's')
    warms = []
    monkeypatch.setattr(lh, '_warm_async',
                        lambda *a, **k: warms.append(a))
    monkeypatch.setattr(lh, '_get', lambda p, caller=None: (_ for _ in ()).throw(
        AssertionError('cached_only darf nie HTTP machen')))
    lh._facts_memo.clear()
    assert lh.lh_flight_facts('LH400', '2026-07-22', cached_only=True) == {}
    assert len(warms) == 1
    # Memo-Hit: liefert die Fakten ohne weiteres Warmup.
    lh._facts_memo[('LH400', '2026-07-22', None, None)] = (
        _t.time() + 60, {'gate': 'C16'})
    assert lh.lh_flight_facts('LH400', '2026-07-22',
                              cached_only=True) == {'gate': 'C16'}
    assert len(warms) == 1


# ─────────────────────────────────────────────────────────────────────────────
# LH-QUOTA-SICHTBARKEIT (2026-07-26) — der prozess-übergreifende Zähler.
# `_HOUR_BUDGET`/`_hour_count` zählen PRO PROZESS (3 Backend-Worker + Poll-
# Worker, kein gemeinsames Volume) — die LH-Quota gilt aber PRO KEY. Deshalb
# ax_api_budget als gemeinsame Wahrheit, aufgeschlüsselt nach Aufrufer.
# ─────────────────────────────────────────────────────────────────────────────

def test_budget_inc_buffers_and_flushes_hour_and_caller_key(monkeypatch):
    import time as _t
    from blueprints import aerox_data_blueprint as adb
    seen = []
    monkeypatch.setattr(adb, '_budget_key_inc',
                        lambda key, units=1: seen.append((key, units)))
    lh._budget_buf.clear()
    lh.budget_inc('lhopen', 'mqtt_event')
    lh.budget_inc('lhopen', 'mqtt_event')
    # GEPUFFERT: im Hot-Path wird NICHT geschrieben (kein Supabase-Roundtrip,
    # kein 120-s-postgrest-Timeout im LH-/MQTT-/Cron-Thread) — erst der
    # Flusher-Thread schreibt, und dann aggregiert.
    assert seen == []
    lh.budget_flush()
    h = _t.strftime('%Y%m%d%H', _t.gmtime())
    # STUNDE VOR AUFRUFER — sonst ist die Abfrage kein index-nutzbares Praefix.
    assert (f'lhopen:{h}', 2) in seen
    assert (f'lhopen:{h}:mqtt_event', 2) in seen
    assert lh._budget_buf == {}


def test_budget_flush_puts_units_back_when_write_fails(monkeypatch):
    from blueprints import aerox_data_blueprint as adb

    def _boom(key, units=1):
        raise RuntimeError('sb down')
    monkeypatch.setattr(adb, '_budget_key_inc', _boom)
    lh._budget_buf.clear()
    lh.budget_inc('lhfo', None)          # darf NICHT werfen
    assert lh.budget_flush() == 0        # auch der Flush nicht
    # Nicht geschriebene Einheiten bleiben erhalten (kein Verlust beim ersten
    # Netzruckler) und werden beim naechsten Versuch erneut geschrieben.
    assert lh._budget_buf
    lh._budget_buf.clear()


def test_budget_caller_label_is_sanitised(monkeypatch):
    from blueprints import aerox_data_blueprint as adb
    seen = []
    monkeypatch.setattr(adb, '_budget_key_inc',
                        lambda key, units=1: seen.append(key))
    lh._budget_buf.clear()
    lh.budget_inc('lhfo', 'COMMON_DUTY:EVENTS/../x')
    lh.budget_flush()
    # Kein ':' im Label — sonst zerfaellt das Key-Parsing in lh_quota_snapshot.
    labels = [k for k in seen if k.count(':') == 2]
    assert labels and all(':' not in k.split(':')[2] for k in labels)
    lh._budget_buf.clear()


def test_denied_calls_are_counted_too(monkeypatch):
    """Der eigene Prozess-Throttle deckelt bei _HOUR_BUDGET — ein reiner
    „gesendet"-Zaehler koennte darum NIE ueber 220xProzesse steigen und waere
    fuer die Frage „warum ueberm Limit?" blind. Abgewiesene mitzaehlen."""
    calls = []
    monkeypatch.setattr(lh, '_token', lambda: 'tok')
    monkeypatch.setattr(lh, '_budget_ok', lambda: False)
    monkeypatch.setattr(lh, 'budget_inc',
                        lambda prefix, caller=None, units=1:
                        calls.append((prefix, caller)))
    assert lh._get('/x', caller='unit') is None
    assert calls and calls[0][0] == 'lhopen_denied'


def test_budget_writes_are_blocked_inside_pytest():
    # Schutz gegen „lokaler Testlauf verfaelscht die Prod-Zaehler".
    assert lh._budget_writes_allowed() is False


def test_get_books_a_call_in_the_shared_counter(monkeypatch):
    """Jeder LH-Call, der einen Slot bekommt, muss gezählt werden — auch wenn
    LH danach 404/403 liefert (der Call ist verbraucht)."""
    calls = []
    monkeypatch.setattr(lh, '_token', lambda: 'tok')
    monkeypatch.setattr(lh, '_budget_ok', lambda: True)
    monkeypatch.setattr(lh, 'budget_inc',
                        lambda prefix, caller=None, units=1:
                        calls.append((prefix, caller)))

    def _boom(*a, **k):
        raise OSError('net')
    monkeypatch.setattr(lh.urllib.request, 'urlopen', _boom)
    assert lh._get('/x', caller='unit') is None
    assert calls == [('lhopen', 'unit')]


def test_get_books_blocked_calls_as_denied_not_as_sent(monkeypatch):
    calls = []
    monkeypatch.setattr(lh, '_token', lambda: 'tok')
    monkeypatch.setattr(lh, '_budget_ok', lambda: False)
    monkeypatch.setattr(lh, 'budget_inc',
                        lambda prefix, caller=None, units=1:
                        calls.append((prefix, caller)))
    assert lh._get('/x', caller='unit') is None
    # Nicht als gesendet buchen (der Call ging nie raus) — aber als ABGEWIESEN,
    # sonst ist der Bedarf unsichtbar.
    assert [c for c in calls if c[0] == 'lhopen'] == []
    # Label = GRUND + AUFRUFER (27.07.): „3.901 mal am Stunden-Budget
    # abgewiesen" sagte nicht, WER den Bedarf erzeugt hat.
    assert calls == [('lhopen_denied', 'hour_budget_unit')]
    assert lh.last_call_denied() is True
    assert lh.last_call_answered() is False


def test_facts_memo_alias_defragments_cache(monkeypatch):
    """MQTT ruft ohne dep/arr, die Roster-Pfade IMMER mit — derselbe Flug belegte
    zwei Memo-Einträge und der teure Push-Call wärmte den falschen Key."""
    lh._facts_memo.clear()
    monkeypatch.setattr(lh, 'lh_open_configured', lambda: True)
    monkeypatch.setattr(lh, '_get', lambda path, caller=None: {'x': 1})
    monkeypatch.setattr(lh, '_leg_to_facts',
                        lambda leg: {'reg': 'D-AIKP', 'dep_iata': 'FRA',
                                     'arr_iata': 'JFK'})
    monkeypatch.setattr(lh, '_budget_ok', lambda: True)

    def _fake_legs(path, caller=None):
        return {'FlightStatusResource': {'Flights': {'Flight': [{'Departure': {}, 'Arrival': {}}]}}}
    monkeypatch.setattr(lh, '_get', _fake_legs)
    out = lh.lh_flight_facts('LH400', '2026-07-26', force=True, caller='mqtt')
    assert out.get('reg') == 'D-AIKP'
    assert ('LH400', '2026-07-26', None, None) in lh._facts_memo
    assert ('LH400', '2026-07-26', 'FRA', 'JFK') in lh._facts_memo
    lh._facts_memo.clear()


# ─────────────────────────────────────────────────────────────────────────────
# PROZESS-ÜBERGREIFENDER GATE (2026-07-27) — Owner: drei Stunden über 1.000/h.
# Der Zähler von 26.07. machte den Verbrauch nur SICHTBAR; gedrosselt wurde
# weiter pro Prozess (220 × 4 Prozesse = 880/h Decke, und kein Prozess wusste
# von den anderen). Jetzt gated `_budget_ok` zusätzlich auf den Gesamtstand,
# den der ohnehin laufende 30-s-Flusher vom atomaren RPC zurückbekommt.
# ─────────────────────────────────────────────────────────────────────────────

def _reset_global_gate():
    lh._global_hour = 0
    lh._global_count = 0
    lh._global_local_since = 0
    lh._global_warned_hour = -1


def test_global_gate_blocks_when_other_processes_used_the_quota():
    import time as _t
    _reset_global_gate()
    h = _t.strftime('%Y%m%d%H', _t.gmtime())
    # Dieser Prozess hat selbst noch NICHTS verbraucht — die anderen aber fast
    # alles. Genau der Fall, den der Pro-Prozess-Zähler nie sehen konnte.
    lh.note_global_budget(h, lh._GLOBAL_HOUR_BUDGET)
    assert lh._global_budget_check(_t.time()) is False
    _reset_global_gate()


def test_global_gate_counts_own_calls_between_flushes():
    import time as _t
    _reset_global_gate()
    h = _t.strftime('%Y%m%d%H', _t.gmtime())
    lh.note_global_budget(h, lh._GLOBAL_HOUR_BUDGET - 2)
    now = _t.time()
    assert lh._global_budget_check(now) is True
    lh._global_budget_commit()
    assert lh._global_budget_check(now) is True
    lh._global_budget_commit()
    # Jetzt ist die Decke rechnerisch erreicht, obwohl seit dem Flush kein
    # neuer Gesamtstand kam — sonst überzieht jeder Prozess 30 s lang blind.
    assert lh._global_budget_check(now) is False
    _reset_global_gate()


def test_global_gate_fails_open_without_shared_number():
    """Ohne Supabase-Stand (frischer Prozess) darf der Gate NICHT dichtmachen —
    dann bleibt `_HOUR_BUDGET` pro Prozess die Bremse. Lieber die alte Decke
    als LH-Enrichment komplett aus."""
    import time as _t
    _reset_global_gate()
    assert lh._global_budget_check(_t.time()) is True
    _reset_global_gate()


def test_flush_feeds_global_gate_from_rpc_total(monkeypatch):
    import time as _t
    from blueprints import aerox_data_blueprint as adb
    _reset_global_gate()
    monkeypatch.setattr(adb, '_budget_key_inc',
                        lambda key, units=1: 777 if key.count(':') == 1 else None)
    lh._budget_buf.clear()
    lh.budget_inc('lhopen', 'mqtt_leg_reg')
    lh.budget_flush()
    assert lh._global_count == 777      # Gesamtstand ALLER Prozesse übernommen
    lh._budget_buf.clear()
    _reset_global_gate()


def test_last_call_answered_separates_outage_from_answer(monkeypatch):
    """404 = „gibt es nicht" (Antwort). 503/Throttle = „wir wissen es nicht".
    Der Unterschied entscheidet, ob ein leeres Ergebnis negativ gecacht
    werden darf."""
    import urllib.error
    monkeypatch.setattr(lh, '_token', lambda: 'tok')
    monkeypatch.setattr(lh, '_budget_ok', lambda: True)
    monkeypatch.setattr(lh, 'budget_inc', lambda *a, **k: None)

    def _raise(code):
        def _open(req, timeout=10):
            raise urllib.error.HTTPError(req.full_url, code, 'x', {}, None)
        return _open

    monkeypatch.setattr(lh.urllib.request, 'urlopen', _raise(404))
    assert lh._get('/x') is None and lh.last_call_answered() is True
    monkeypatch.setattr(lh.urllib.request, 'urlopen', _raise(503))
    assert lh._get('/x') is None and lh.last_call_answered() is False


def test_denied_call_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(lh, '_token', lambda: 'tok')
    monkeypatch.setattr(lh, '_budget_ok', lambda: False)
    monkeypatch.setattr(lh, 'budget_inc', lambda *a, **k: None)
    assert lh._get('/x') is None
    assert lh.last_call_answered() is False


# ── Fakten-TTL nach Abflugnähe (Quota-Runde 2 · 2026-07-27) ─────────────────
# Nach dem Reg-Cache-Fix war die obs_*-Familie der nächstgrösste Verbraucher
# des Open-API-Keys (397/h in Stunde 08 UTC, 620/h in Stunde 07). Ursache: die
# FLACHE 120-s-TTL für alles, was „heute" ist — während der Roster-Warmer alle
# 30 min bis zu 500 Flüge von heute UND morgen vorrechnet, deren Abflug meist
# Stunden weg ist.

def _ttl(date_str, facts, at='2026-07-27T09:00:00'):
    import calendar
    import time as _t
    now = calendar.timegm(_t.strptime(at, '%Y-%m-%dT%H:%M:%S'))
    return lh._facts_ttl(date_str, facts, now)


def test_facts_ttl_other_day_unchanged():
    assert _ttl('2026-07-28', {'sched_dep': '2026-07-28T10:00:00+02:00'}) == 6 * 3600
    assert _ttl('2026-07-26', {'sched_dep': '2026-07-26T10:00:00+02:00'}) == 6 * 3600


def test_facts_ttl_falls_back_to_the_old_120s_when_in_doubt():
    """Keine Fakten, keine Zeiten, oder eine Zeit OHNE Zone (LH lieferte kein
    UTC → `_offset_iso` gibt naives Lokal zurück, das nicht mit „jetzt"
    vergleichbar ist) → unverändertes Altverhalten."""
    assert _ttl('2026-07-27', {}) == 120
    assert _ttl('2026-07-27', {'gate': 'A1'}) == 120
    assert _ttl('2026-07-27', {'sched_dep': '2026-07-27T17:00:00'}) == 120


def test_facts_ttl_inside_the_operating_window_stays_short():
    """Ab 2 h vor Abflug bis 2 h nach Ankunft bleibt alles wie bisher — genau
    hier wechseln Gate und Ist-Zeiten."""
    ops = {'sched_dep': '2026-07-27T10:00:00+00:00',
           'sched_arr': '2026-07-27T12:00:00+00:00'}
    assert _ttl('2026-07-27', ops) == 120
    # 1 h nach der Ankunft: immer noch operativ (est_arr kann eine zu
    # optimistische SCHÄTZUNG sein, der Flug also noch in der Luft).
    landed = {'sched_dep': '2026-07-27T06:00:00+00:00',
              'est_arr': '2026-07-27T08:00:00+00:00'}
    assert _ttl('2026-07-27', landed) == 120


def test_facts_ttl_far_before_departure_never_outlives_the_window_start():
    """Weit vor dem Abflug länger halten — aber NIE über den Beginn des
    Betriebsfensters (Abflug − 2 h) hinaus, damit der erste Read danach
    garantiert frische Gate-/Ist-Daten holt."""
    far = {'sched_dep': '2026-07-27T17:00:00+00:00'}     # +8 h
    assert _ttl('2026-07-27', far) == 20 * 60
    near = {'sched_dep': '2026-07-27T11:10:00+00:00'}    # +2 h 10 min
    assert _ttl('2026-07-27', near) == 10 * 60           # exakt bis 09:10 +2h
    # Kurz VOR dem Fenster darf die TTL nie UNTER die Fenster-TTL rutschen —
    # sonst wären es mehr Calls als vorher, nicht weniger.
    assert _ttl('2026-07-27', {'sched_dep': '2026-07-27T11:00:30+00:00'}) == 120


def test_facts_ttl_long_after_arrival():
    """Erst 2 h nach der letzten bekannten Ankunftszeit gilt ein Flug als
    fertig — dann sind die Ist-Zeiten final."""
    done = {'sched_dep': '2026-07-27T04:00:00+00:00',
            'est_arr': '2026-07-27T06:00:00+00:00'}
    assert _ttl('2026-07-27', done) == 30 * 60


def test_memo_hit_counts_as_an_answer(monkeypatch):
    """Ein Memo-Treffer IST eine Antwort. Ohne das läse ein Aufrufer wie
    `lh_mqtt._fetch_leg_reg` nach einem Cache-Hit den answered/denied-Zustand
    eines längst vergangenen GETs desselben Threads."""
    monkeypatch.setattr(lh, '_KEY', 'k')
    monkeypatch.setattr(lh, '_SECRET', 's')
    monkeypatch.setattr(lh, '_get', lambda *a, **k: None)
    lh._facts_memo.clear()
    lh._facts_memo[('LH400', '2026-07-27', 'FRA', 'JFK')] = (
        time.time() + 600, {'reg': 'D-AIKP'})
    lh._call_state.answered = False
    lh._call_state.denied = True
    out = lh.lh_flight_facts('LH400', '2026-07-27', 'FRA', 'JFK', caller='unit')
    assert out == {'reg': 'D-AIKP'}
    assert lh.last_call_answered() is True
    assert lh.last_call_denied() is False
    lh._facts_memo.clear()
