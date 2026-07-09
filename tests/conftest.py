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
from pathlib import Path

import pytest

os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """Modul-globale TTL-Caches vor JEDEM Test leeren (Test-Isolation).

    In Produktion sind diese Caches korrekt (per Token/Datum, kurze TTL) — ein
    Feed-Aufruf pro 90 s soll denselben Stand wiederverwenden. Im Test aber würde
    der gecachte Response eines vorigen Tests einen späteren Test mit demselben
    Token/Datum fälschlich mit stale Daten beantworten. Produkt-Code bleibt
    unverändert; nur die geteilte Modul-State wird zwischen Tests zurückgesetzt."""
    with contextlib.suppress(Exception):
        import app
        for _name in ('_FRIENDS_TODAY_MEMO',):
            _c = getattr(app, _name, None)
            if isinstance(_c, dict):
                _c.clear()
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
