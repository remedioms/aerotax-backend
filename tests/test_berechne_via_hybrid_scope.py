"""Regression: _berechne_via_hybrid name-scope (2026-05-23).

Hintergrund: v14 Release-Blocker-Commit 42dddc1 fügte _homebase_audit ins
return-dict von _berechne_via_hybrid ein und referenzierte dort `homebase`,
das in dieser Funktion nie definiert war. Resultat: NameError im Live-Run
nach 4 Minuten Pipeline-Arbeit (Tibor job 7cf07e04, 2026-05-23 22:51).

Dieser Test fängt diese Klasse Bug:
1. Statisch: alle Free-Variables von _berechne_via_hybrid sind im Modul-Scope
   verfügbar (= keine NameError zur Runtime).
2. Funktional: _berechne_via_hybrid mit minimal-validem hybrid_analyze-Mock
   läuft durch ohne NameError.
"""
import builtins
import inspect
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('AEROTAX_ALLOW_BOOT_WITHOUT_KEY', '1')

import app  # noqa: E402

_BUILTIN_NAMES = set(dir(builtins))


def test_berechne_via_hybrid_free_vars_resolvable():
    """Statisch: jede Free-Variable von _berechne_via_hybrid ist im Modul-Scope.

    AST-basiert: sammelt nur echte ast.Name(Load)-Referenzen (also tatsächliche
    Variable-Lookups), nicht Attribute-Access (.get, .upper). Fängt die NameError-
    Klasse ab — z.B. der homebase-Bug, der bei 42dddc1 reingerutscht ist.
    """
    import ast
    import textwrap
    src = textwrap.dedent(inspect.getsource(app._berechne_via_hybrid))
    tree = ast.parse(src)
    fn_node = tree.body[0]
    assert isinstance(fn_node, ast.FunctionDef)

    # Sammle alle lokalen Bindings: Args + alle Assignment-Targets (rekursiv,
    # inkl. Tuple-Unpacking, For-Loop-Vars, With-Vars, Comprehensions).
    local_names = set()
    for arg in fn_node.args.args:
        local_names.add(arg.arg)
    if fn_node.args.vararg:
        local_names.add(fn_node.args.vararg.arg)
    if fn_node.args.kwarg:
        local_names.add(fn_node.args.kwarg.arg)
    for node in ast.walk(fn_node):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                for sub in ast.walk(t):
                    if isinstance(sub, ast.Name):
                        local_names.add(sub.id)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    local_names.add(sub.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if item.optional_vars:
                    for sub in ast.walk(item.optional_vars):
                        if isinstance(sub, ast.Name):
                            local_names.add(sub.id)
        elif isinstance(node, (ast.comprehension,)):
            for sub in ast.walk(node.target):
                if isinstance(sub, ast.Name):
                    local_names.add(sub.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            local_names.add(node.name)
        elif isinstance(node, ast.FunctionDef):
            if node is not fn_node:
                local_names.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                local_names.add(alias.asname or alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                local_names.add(alias.asname or alias.name)

    # Sammle alle Name(Load) Referenzen — echte Lookups, keine Attribute
    name_loads = set()
    for node in ast.walk(fn_node):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name_loads.add(node.id)

    missing = []
    module_attrs = set(dir(app))
    for name in name_loads:
        if name in local_names:
            continue
        if name in module_attrs:
            continue
        if name in _BUILTIN_NAMES:
            continue
        missing.append(name)

    assert not missing, (
        f"_berechne_via_hybrid referenziert {sorted(missing)} — diese Namen sind weder "
        f"Argument, lokale Variable noch Modul-Member. Wahrscheinlich Scope-Lücke "
        f"(siehe homebase-Bug 42dddc1 → 7272e82)."
    )


def test_berechne_via_hybrid_smoke_no_nameerror():
    """Funktional: ein realistischer Aufruf wirft keinen NameError.

    Wir mocken hybrid_analyze damit wir nicht die ganze Pipeline laufen lassen,
    und prüfen nur dass der Funktionskörper bis zum return durchläuft.
    """
    fake_hr = {
        'lsb': {
            'brutto': 60000.0, 'lohnsteuer': 8000.0, 'soli': 0.0,
            'kirchensteuer_an': 0.0, 'ag_fahrt_z17': 0.0,
            'ag_fahrt_z18_pauschal': 0.0, 'verpflegungszuschuss_z20': 0.0,
            'rv_an': 5000.0, 'rv_ag': 5000.0, 'kv_an': 4000.0,
            'pv_an': 500.0, 'av_an': 800.0, 'vorsorge_gesamt_an': 1000.0,
            'rv_gesamt': 10000.0, 'identnr': '12345678901',
            'geburtsdatum': '1990-01-01', 'personalnummer': '987654',
            'arbeitgeber': 'Deutsche Lufthansa AG', 'steuerklasse': '1',
            'kinderfreibetraege': 0.0,
        },
        'classification': {
            'arbeitstage': 180, 'reinigungstage': 150, 'fahr_tage': 100,
            'hotel_naechte': 60, 'tage_detail': [],
        },
        'se_summary': {
            'z77_total': 5000.0, 'summe_gesamt': 5000.0,
            'summe_steuerpflichtig': 0.0,
        },
        'errors': [],
    }

    form = {
        'year': '2025',
        'base': 'Frankfurt (FRA)',
        'km': '15',
        'anfahrt_min': '20',
        'homebase': 'FRA',
    }
    files = {}

    with patch.object(app, 'hybrid_analyze', return_value=fake_hr):
        # Smoke: nicht crashen mit NameError. result kann None sein wenn
        # arbeitstage==0 oder ähnliches, das ist OK — nur kein NameError.
        try:
            result = app._berechne_via_hybrid(form, files, job_id='test-smoke')
        except NameError as e:
            pytest.fail(f"_berechne_via_hybrid wirft NameError: {e}")
        # result darf None sein (z.B. keine Klassifikation), darf aber kein Crash sein
        assert result is None or isinstance(result, dict)


def test_berechne_via_hybrid_homebase_resolves_from_form():
    """Konkret: homebase wird aus form abgeleitet, nicht referenziert ohne Definition."""
    fake_hr = {
        'lsb': {'brutto': 50000.0, 'lohnsteuer': 5000.0},
        'classification': {
            'arbeitstage': 200, 'reinigungstage': 180, 'fahr_tage': 110,
            'hotel_naechte': 70, 'tage_detail': [],
        },
        'se_summary': {'z77_total': 3000.0},
        'errors': [],
    }

    form = {'year': '2025', 'base': 'München (MUC)'}
    files = {}

    with patch.object(app, 'hybrid_analyze', return_value=fake_hr):
        result = app._berechne_via_hybrid(form, files, job_id='test-hb')

    if result is not None:
        # _homebase_audit muss MUC enthalten — bestätigt dass die Variable
        # korrekt aus form abgeleitet wird
        hb_audit = result.get('_homebase_audit') or {}
        assert hb_audit.get('iata') == 'MUC', (
            f"homebase wurde nicht aus form abgeleitet, audit={hb_audit}"
        )
