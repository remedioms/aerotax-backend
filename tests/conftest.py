"""Pytest conftest — ENV defaults für ALL Test-Imports.

P0 #10 Fix: app.py macht beim Import einen Boot-Check für RECOVERY_SECRET.
Ohne Flag würden alle Tests beim `import app` crashen. Tests laufen damit
explizit im non-production-Modus.

Pfad-Auflösung (2026-07-01): Das Repo ist von ~/Desktop/aerotax-backend nach
~/Developer/Backend/aerotax-backend umgezogen. Tests dürfen KEINE hartkodierten
absoluten Pfade mehr enthalten — stattdessen:

  - BACKEND_ROOT / backend_path(...)  → Repo-Root, relativ zu dieser Datei
                                         (Override: env AEROTAX_BACKEND_ROOT)
  - SITE_INDEX_HTML / site_index_html() → aerosteuer.de-Frontend index.html
                                         (Override: env AEROTAX_SITE_ROOT).
    site_index_html() skippt den aufrufenden Test (bzw. das Modul bei
    module-level Aufruf), wenn das site-Repo nicht auf der Platte liegt.
"""
import contextlib
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')
# Legacy endpoint tests predate ubiquitous Authorization headers and exercise
# route business logic, not binding. Production defaults to enforced; dedicated
# auth contract tests cover that default and strict behavior explicitly.
os.environ.setdefault('AEROX_REQUIRE_TOKEN_BINDING', '0')


def _loaded_module_references():
    """Return current modules plus stale module refs retained by test modules."""
    import sys
    import types

    modules = []
    seen = set()

    def add(module):
        if not isinstance(module, types.ModuleType) or id(module) in seen:
            return
        seen.add(id(module))
        modules.append(module)

    for module in list(sys.modules.values()):
        add(module)
    # `test_calculation.py` swaps sys.modules['app']; already collected tests
    # still retain the original module under names such as A/backend.
    # Only test modules can retain the deliberately swapped ``app`` module.
    # Scanning every imported library's globals for every test made the full
    # suite needlessly CPU-heavy.
    test_modules = [
        module for name, module in list(sys.modules.items())
        if name.startswith('test') or name.startswith('tests.')
    ]
    for container in test_modules:
        with contextlib.suppress(Exception):
            for value in vars(container).values():
                if (isinstance(value, types.ModuleType)
                        and (getattr(value, '__name__', '') == 'app'
                             or hasattr(value, '_bug004_route_owner_token'))):
                    add(value)
    return modules


# Memoisierte Modul-Globals, die zwischen Tests geleert werden müssen.
# Wert = optionaler Struktur-Seed, der nach clear() wieder eingesetzt wird
# (Caches mit Pflicht-Keys wie _AX_CODESHARE_CACHE['ts']/['map'] dürfen nicht
# leer zurückbleiben). Heimat-Module (nur Doku — geleert wird via sys.modules-
# Scan, s.u.):
#   app: _FRIENDS_TODAY_MEMO, _PROFILE_HB_MEMO, _TRIP_STATS_CACHE,
#        _FLIGHT_MERGE_CACHE, _AIRPORT_BOARD_CACHE, _AIRPORT_DAY_CACHE,
#        _NATIVE_BOARD_CACHE, _AX_CODESHARE_CACHE
#   blueprints.aerox_data_blueprint: _UFLIGHT_MEMO, _LIFECYCLE_MEMO
_MEMO_GLOBALS = {
    '_FRIENDS_TODAY_MEMO': None,
    '_ROUTE_HISTORY_MEMO': None,
    '_PROFILE_HB_MEMO': None,
    '_TRIP_STATS_CACHE': None,
    '_FLIGHT_MERGE_CACHE': None,
    '_AIRPORT_BOARD_CACHE': None,
    '_AIRPORT_DAY_CACHE': None,
    '_NATIVE_BOARD_CACHE': None,
    '_AX_CODESHARE_CACHE': {'ts': 0.0, 'map': {}},
    '_PASSPORT_STATS_CACHE': None,
    '_PASSPORT_ROUTE_DUR_CACHE': None,
    '_UFLIGHT_MEMO': None,
    '_LIFECYCLE_MEMO': None,
    '_FREE_TIMES_MEMO': None,
    '_FREE_CREW_LIVE_MEMO': None,
    '_OBS_FACTS_MEMO': None,
    '_BRIEFING_RESPONSE_MEMO': None,
    '_WALL_FEED_MEMO': None,
    '_AIRPORT_BOARD_RESPONSE_MEMO': None,
    # Sperrfrist nach fehlgeschlagenem Daily-Briefing (blueprints/daily_briefing).
    # In Produktion gewollt: ein Fehlschlag drosselt Retries für 120 s, damit
    # Client-Wiederholungen nicht die volle LH-Call-Kette erneut bezahlen. Im
    # Test würde derselbe (Token, Tag) aus einem vorigen Test die folgenden
    # Tests in die Sperrfrist laufen lassen.
    '_fail_memo': None,
}


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Modul-globale TTL-Caches vor JEDEM Test leeren (Test-Isolation).

    In Produktion sind diese Caches korrekt (per Token/Datum, kurze TTL) — ein
    Feed-Aufruf pro 90 s soll denselben Stand wiederverwenden. Im Test aber würde
    der gecachte Response eines vorigen Tests einen späteren Test mit demselben
    Token/Datum fälschlich mit stale Daten beantworten. Produkt-Code bleibt
    unverändert; nur die geteilte Modul-State wird zwischen Tests zurückgesetzt."""
    # Robust ggü. dem sys.modules['app']-Swap (test_calculation.py reimportet app):
    # die Caches auf JEDEM Modul leeren, das sie trägt (Original UND Reimport).
    # Existenz defensiv (getattr + isinstance) — fehlende Attribute sind ok.
    import copy
    for _mod in _loaded_module_references():
        for _name, _seed in _MEMO_GLOBALS.items():
            with contextlib.suppress(Exception):
                _c = getattr(_mod, _name, None)
                if isinstance(_c, dict):
                    _c.clear()
                    if _seed:
                        _c.update(copy.deepcopy(_seed))
        # Token-gate tests must not depend on a user created by an earlier
        # test run remaining in the worktree's disk cache. Treat the current
        # disk snapshot (including an honestly empty one) as an available
        # local auth store for each test. Tests for real store outages replace
        # this state explicitly. This keeps a clean checkout deterministic
        # without weakening the production fail-closed tri-state behavior.
        with contextlib.suppress(Exception):
            _token_cache = getattr(_mod, '_TOKEN_VALIDATE_CACHE', None)
            _disk_loader = getattr(_mod, '_auth_load_from_disk', None)
            if isinstance(_token_cache, dict) and callable(_disk_loader):
                _users = _disk_loader() or {}
                _tokens = {
                    str(_record.get('token')): str(_email)
                    for _email, _record in _users.items()
                    if isinstance(_record, dict) and _record.get('token')
                }
                _now = time.time()
                _token_cache['tokens'] = _tokens
                _token_cache['expires'] = _now + 60.0
                _token_cache['last_force'] = _now
    yield


@pytest.fixture(autouse=True)
def _legacy_business_route_tokens(monkeypatch):
    """Keep pre-auth business tests focused on their route logic.

    The production owner gate now correctly rejects public ``AXU-*`` refs and
    validates every real ``AT-*`` account token.  A large part of the older
    business-logic suite deliberately uses readable placeholders such as
    ``testtoken123``; before the owner-slot hardening those values never entered
    the global auth gate.  Preserve that test contract without weakening the
    production implementation: only pytest's loaded app module is patched,
    and security-shaped ``AT-*``/``AXU-*`` owners still take the real gate.
    """
    # `test_calculation.py` re-importiert `app` absichtlich. Bereits geladene
    # Testmodule behalten danach jedoch ihre alte `A`/`backend`-Modulreferenz.
    # Deshalb neben sys.modules auch direkte Modulwerte der Testmodule prüfen;
    # so bekommen beide Flask-App-Instanzen denselben pytest-only Wrapper.
    for module in _loaded_module_references():
        original = getattr(module, '_bug004_route_owner_token', None)
        token_re = getattr(module, '_BUG004_TOKEN_PATH_RE', None)
        if not callable(original) or token_re is None:
            continue

        def legacy_owner(path, _original=original, _token_re=token_re):
            owner = _original(path)
            if owner is None:
                return None
            value = str(owner).strip()
            if value.startswith('AXU-') or _token_re.fullmatch('/' + value):
                return owner
            return None

        monkeypatch.setattr(module, '_bug004_route_owner_token', legacy_owner)

    yield

# ─── Backend-Root (dieses Repo) ──────────────────────────────────────────────
BACKEND_ROOT = Path(
    os.environ.get('AEROTAX_BACKEND_ROOT')
    or Path(__file__).resolve().parents[1]
)


def backend_path(*parts) -> str:
    """Absoluter Pfad zu einer Datei im Backend-Repo (z.B. backend_path('app.py'))."""
    return str(BACKEND_ROOT.joinpath(*parts))


# ─── Site-Repo (statisches aerosteuer.de-Frontend) ───────────────────────────
def _resolve_site_root():
    env = os.environ.get('AEROTAX_SITE_ROOT')
    if env:
        return Path(env)
    home = Path.home()
    candidates = (
        home / 'Desktop' / 'AeroTax' / 'site',   # Stand 2026-07-01
        home / 'Desktop' / 'site',               # Alt (vor Desktop-Aufräumung)
        home / 'Developer' / 'site',
        home / 'Developer' / 'AeroTax' / 'site',
    )
    for c in candidates:
        if (c / 'index.html').is_file():
            return c
    return None


SITE_ROOT = _resolve_site_root()

# Plain-String-Konstante für module-level Zuweisungen in gemischten Modulen
# (dort darf ein fehlendes site-Repo nicht das ganze Modul skippen).
SITE_INDEX_HTML = str((SITE_ROOT or Path.home() / 'Desktop' / 'AeroTax' / 'site') / 'index.html')


# ─── Private Original-PDFs (Miguel/Tibor, liegen NICHT im Repo) ──────────────
def _resolve_private_docs_root():
    env = os.environ.get('AEROTAX_PRIVATE_DOCS_ROOT')
    if env:
        return Path(env)
    home = Path.home()
    candidates = (
        home / 'Desktop' / 'Downloads',   # Stand 2026-07-01
        home / 'Downloads',
    )
    for c in candidates:
        if (c / 'Tibor').is_dir() or (c / 'Steuer 25').is_dir():
            return c
    return None


PRIVATE_DOCS_ROOT = _resolve_private_docs_root()


def private_doc(*parts) -> str:
    """Pfad zu einer privaten Original-PDF (z.B. private_doc('Tibor', '2025', ...)).

    Existenz wird NICHT geprüft — die aufrufenden Tests skippen selbst via
    os.path.exists/isdir, damit fehlende Privat-Daten (CI) nur skippen.
    """
    base = PRIVATE_DOCS_ROOT or Path.home() / 'Desktop' / 'Downloads'
    return str(base.joinpath(*parts))


def site_index_html() -> str:
    """Pfad zur Frontend-index.html — skippt sauber, wenn das site-Repo fehlt.

    Funktioniert sowohl innerhalb einer Testfunktion (skippt den Test) als auch
    auf Modulebene in reinen Frontend-DOM-Testmodulen (skippt das Modul).
    """
    if SITE_ROOT is None or not (SITE_ROOT / 'index.html').is_file():
        import pytest
        pytest.skip('site repo not found', allow_module_level=True)
    return str(SITE_ROOT / 'index.html')
