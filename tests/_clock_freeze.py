"""Deterministische Test-Uhr — geteilte Mechanik (Test-Infra, 2026-07-27).

Heimat war test_leg_delay_enrichment.py (dort 2026-07-27 gebaut); jetzt von
allen wanduhr-sensiblen Suiten genutzt (test_leg_delay_enrichment,
test_overnight_spillover, test_crew_live_state, test_my_flight_status).

WARUM: Diese Module bauen ihre Sektor-Zeiten RELATIV zu „jetzt" und ihre
Roster-Tages-Keys aus `date.today()`. Die Produktion ankert dagegen auf dem
BERLINER Betriebstag (`_airport_local_now('FRA')` in get_friends_today) bzw.
auf der PROZESS-lokalen Uhr (`date.today()` im today±1-Guard von
`_enrich_leg_delays`). Sobald diese Uhren auf VERSCHIEDENE Kalendertage
zeigen — zwischen 22:00 und 24:00 UTC ist Europe/Berlin schon am Folgetag —
passte der Tages-Key des Tests nicht mehr zum „heute" der Produktion und die
Suiten waren reproduzierbar rot.

LÖSUNG: EIN fixer absoluter Zeitpunkt für ALLE Uhren, die die Tests erreichen.
Absolut (nicht „heute um 12:00"), damit die Tests auch vom echten Kalender
unabhängig sind.

10:00 UTC ist bewusst gewählt: das ist 12:00 in Europe/Berlin und liegt für
JEDE Prozess-Zeitzone von UTC−10 bis UTC+13 noch auf DEMSELBEN Kalendertag
(UTC 10:00 / Berlin 12:00 / Pacific/Auckland 22:00 / America/Los_Angeles
03:00). Damit gilt: UTC-Datum == Berlin-Datum == Prozess-lokales Datum.

VERWENDUNG (pro Testmodul, autouse):

    import sys
    from _clock_freeze import FROZEN_UTC, apply_frozen_clock

    @pytest.fixture(autouse=True)
    def _freeze_clock(monkeypatch):
        apply_frozen_clock(monkeypatch, extra_modules=(sys.modules[__name__],))
        yield

`extra_modules` friert zusätzlich die modul-globalen Namen `datetime`/`date`/
`_date` des TESTmoduls selbst ein — nötig, weil `from datetime import date as
_date` zur Import-Zeit bindet und der Patch auf das stdlib-Modul diese schon
gebundenen Namen nicht mehr erreicht. Nur so sehen Test-Eingaben
(`datetime.now(...)`, `_date.today()`) und App dieselbe eingefrorene Uhr.
"""
from datetime import date as _real_date
from datetime import datetime as _real_datetime
from datetime import timezone

FROZEN_UTC = _real_datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone.utc)
FROZEN_EPOCH = FROZEN_UTC.timestamp()
FROZEN_DATE = FROZEN_UTC.date()


class _FrozenDTMeta(type(_real_datetime)):
    """isinstance(x, _FrozenDT) muss auch für ECHTE datetime-Instanzen wahr
    sein: die Frozen-Klasse wird auf das STDLIB-Modul gepatcht
    (`datetime.datetime = _FrozenDT`), und Produktions-Guards wie
    `isinstance(s, _dt.datetime)` in crew_live_state._parse_iso dürfen plain
    datetimes (z.B. explizit gebaute Test-`now`s) nicht plötzlich ablehnen —
    sonst fiele `now` still auf die Uhr-Fallback-Zeile zurück."""

    def __instancecheck__(cls, inst):
        return isinstance(inst, _real_datetime)


class _FrozenDT(_real_datetime, metaclass=_FrozenDTMeta):
    """`datetime`-Ersatz mit eingefrorenem `now()`/`utcnow()`/`today()`.

    `now(None)` liefert — wie schon in den früheren handgerollten Fixtures
    von test_leg_delay_enrichment — den aware UTC-Zeitpunkt; alle
    Produktions-Callsites auf dem Testpfad übergeben ohnehin eine tz.
    """

    @classmethod
    def now(cls, tz=None):
        return FROZEN_UTC if tz is None else FROZEN_UTC.astimezone(tz)

    @classmethod
    def utcnow(cls):
        return FROZEN_UTC.replace(tzinfo=None)

    @classmethod
    def today(cls):
        return FROZEN_UTC.replace(tzinfo=None)


class _FrozenDateMeta(type(_real_date)):
    """s. _FrozenDTMeta — gleiche Regel für `isinstance(x, _dt.date)`."""

    def __instancecheck__(cls, inst):
        return isinstance(inst, _real_date)


class _FrozenDate(_real_date, metaclass=_FrozenDateMeta):
    """`date`-Ersatz mit eingefrorenem `today()`.

    Wird auf das STDLIB-Modul `datetime` gepatcht, weil die Produktion `date`
    fast überall funktions-lokal importiert (`from datetime import date as
    _date` in `get_friends_today`, `_dt_date` im today±1-Guard von
    `_enrich_leg_delays`) — ein Patch auf `app.<name>` erreicht die nicht.
    """

    @classmethod
    def today(cls):
        return cls(FROZEN_DATE.year, FROZEN_DATE.month, FROZEN_DATE.day)


class _FrozenTimeModule:
    """Proxy auf das echte `time`-Modul; NUR `time()` ist eingefroren.

    `monotonic`/`gmtime`/`strftime`/… bleiben echt (TTL-Caches und
    `time.gmtime(time.time() - 86400)`-Konstrukte funktionieren dadurch
    unverändert, bekommen aber den eingefrorenen Epoch-Wert)."""

    def __init__(self, real):
        self._real = real

    def time(self):
        return FROZEN_EPOCH

    def __getattr__(self, name):
        return getattr(self._real, name)


def apply_frozen_clock(monkeypatch, extra_modules=(), app_module=None):
    """Friert JEDE Uhr ein, die die Suiten erreichen — nicht nur eine.

    Abgedeckt:
      • `app.datetime`            → now()/utcnow()  (u.a. `_airport_local_now`,
                                    `_fra_local_now`, `_enrich_leg_delays`)
      • stdlib `datetime.date`    → today()  (funktions-lokale Importe in der
                                    Produktion: `get_friends_today`-Berlin-
                                    Fallback, today±1-Guard)
      • stdlib `datetime.datetime`→ now()  (funktions-lokale MODUL-Importe:
                                    `import datetime as _cy_dt` im Overnight-
                                    Carry-Block von get_friends_today,
                                    `_dt.datetime.now(...)` in family_watch/
                                    crew_live_state — ein `app.datetime`-Patch
                                    erreicht die nicht; isinstance bleibt dank
                                    _FrozenDTMeta für plain datetimes wahr)
      • `time.time()` in app + den Blueprints (leg_status_gate-Landed-Gate,
        aerox_data_blueprint-Caches/Cutoffs, crew_live_state) über einen
        Modul-Proxy — das echte `time`-Modul bleibt für pytest unangetastet.
      • `extra_modules`: modul-globale Namen `datetime`/`date`/`_date`, sofern
        sie auf die echten stdlib-Klassen zeigen (gedacht für die Testmodule
        selbst, s. Modul-Docstring).
      • `app_module`: das `app`-Modul-OBJEKT des Testmoduls (`import app as A`
        zur Collection-Zeit). PFLICHT im Full-Run-Kontext: test_calculation
        tauscht sys.modules['app'] per Reimport-Trick aus (s. die
        _pin_app_module-Fixtures) — ein `import app` HIER erwischte dann die
        falsche Kopie, und die Uhr des tatsächlich getesteten Moduls bliebe
        ungefroren (genau so 39× in test_leg_delay_enrichment gesehen, nur im
        Full-Run). Es werden das übergebene Objekt UND — falls abweichend —
        die aktuelle sys.modules['app']-Kopie gefroren.

    Alle Patches laufen über `monkeypatch` und werden von pytest automatisch
    zurückgerollt.
    """
    import datetime as _dtmod
    import sys as _sysmod
    import time as _timemod

    _apps = []
    if app_module is not None:
        _apps.append(app_module)
    _sm_app = _sysmod.modules.get('app')
    if _sm_app is None and app_module is None:
        import app as _sm_app  # noqa: F811 — Erst-Import außerhalb pytest
    if _sm_app is not None and all(_sm_app is not a for a in _apps):
        _apps.append(_sm_app)

    monkeypatch.setattr(_dtmod, 'date', _FrozenDate)
    monkeypatch.setattr(_dtmod, 'datetime', _FrozenDT)

    _shim = _FrozenTimeModule(_timemod)
    for _A in _apps:
        monkeypatch.setattr(_A, 'datetime', _FrozenDT)
        if getattr(_A, 'time', None) is _timemod:
            monkeypatch.setattr(_A, 'time', _shim)
    for _modname in ('blueprints.leg_status_gate',
                     'blueprints.aerox_data_blueprint',
                     'blueprints.crew_live_state',
                     # 2026-08-09 nachgezogen: die FlightState-Engine entscheidet
                     # AIRBORNE u.a. über das ALTER der Positions-Beobachtung
                     # (`now = now or time.time()`). Ohne diese Uhr galt jeder
                     # datierte Snapshot am Tag NACH dem Testtag als stale — die
                     # Pflicht-Gegenprobe „ein wirklich fliegender Flug bleibt
                     # airborne" (test_live_pos_instance_binding) kippte darum
                     # 24 h nach ihrer Entstehung von selbst um. Eine Zeitbombe,
                     # kein Produktfehler.
                     'blueprints.flight_state'):
        try:
            _m = __import__(_modname, fromlist=['*'])
        except Exception:
            continue
        if getattr(_m, 'time', None) is _timemod:
            monkeypatch.setattr(_m, 'time', _shim)

    for _mod in extra_modules:
        for _name in ('datetime', 'date', '_date'):
            _cur = getattr(_mod, _name, None)
            if _cur is _real_datetime:
                monkeypatch.setattr(_mod, _name, _FrozenDT)
            elif _cur is _real_date:
                monkeypatch.setattr(_mod, _name, _FrozenDate)
