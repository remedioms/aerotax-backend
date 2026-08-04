"""Remote-Config / Kill-Switches: GET /api/app-config (status_blueprint).

Vertrag:
- Datei-Defaults (config/app_flags.json) werden ausgeliefert, `_`-Keys nicht.
- Env AEROX_FLAGS_JSON überlagert die Datei (Notfall-Hebel ohne Code).
- Kaputte Env wird still ignoriert — Datei-Stand gilt weiter.
- Antwort trägt rev-Hash + Cache-Control (5 min) und ist ohne Auth lesbar.
"""
import importlib
import json

import pytest


@pytest.fixture()
def client(monkeypatch, tmp_path):
    import blueprints.status_blueprint as sbmod

    # Frisch importieren wäre teuer (Flask-App); stattdessen den Modul-Cache
    # zurücksetzen, damit jeder Test die aktuelle Env/Datei sieht.
    sbmod._FLAGS_CACHE.clear()

    from flask import Flask
    app = Flask(__name__)
    app.register_blueprint(sbmod.status_bp)
    yield app.test_client(), sbmod, monkeypatch, tmp_path
    sbmod._FLAGS_CACHE.clear()


def _get(client_tuple, env=None, flags_file=None):
    c, sbmod, monkeypatch, tmp_path = client_tuple
    if flags_file is not None:
        p = tmp_path / "app_flags.json"
        p.write_text(json.dumps(flags_file), encoding="utf-8")
        monkeypatch.setattr(sbmod, "_FLAGS_PATH", str(p))
    if env is not None:
        monkeypatch.setenv("AEROX_FLAGS_JSON", env)
    else:
        monkeypatch.delenv("AEROX_FLAGS_JSON", raising=False)
    sbmod._FLAGS_CACHE.clear()
    return c.get("/api/app-config")


def test_defaults_from_file_are_served(client):
    r = _get(client, flags_file={"layover_roster_wins": True, "_comment": "x"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["flags"]["layover_roster_wins"] is True
    assert "_comment" not in body["flags"]
    assert isinstance(body["rev"], str) and len(body["rev"]) == 12


def test_env_overlay_wins_over_file(client):
    r = _get(client, env='{"layover_roster_wins": false}',
             flags_file={"layover_roster_wins": True})
    assert r.get_json()["flags"]["layover_roster_wins"] is False


def test_broken_env_is_ignored(client):
    r = _get(client, env='{kaputt', flags_file={"layover_roster_wins": True})
    assert r.get_json()["flags"]["layover_roster_wins"] is True


def test_missing_file_yields_empty_flags_not_error(client):
    c, sbmod, monkeypatch, tmp_path = client
    monkeypatch.setattr(sbmod, "_FLAGS_PATH", str(tmp_path / "gibtsnicht.json"))
    monkeypatch.delenv("AEROX_FLAGS_JSON", raising=False)
    sbmod._FLAGS_CACHE.clear()
    r = c.get("/api/app-config")
    assert r.status_code == 200
    assert r.get_json()["flags"] == {}


def test_cache_control_header_set(client):
    r = _get(client, flags_file={"a": 1})
    assert "max-age=300" in r.headers.get("Cache-Control", "")


def test_rev_changes_with_flags(client):
    r1 = _get(client, flags_file={"layover_roster_wins": True})
    rev1 = r1.get_json()["rev"]
    r2 = _get(client, flags_file={"layover_roster_wins": False})
    assert r2.get_json()["rev"] != rev1


def test_real_repo_defaults_parse_and_contain_layover_flag():
    """Die eingecheckte config/app_flags.json ist gültig und trägt das Flag."""
    import blueprints.status_blueprint as sbmod
    with open(sbmod._FLAGS_PATH, encoding="utf-8") as f:
        raw = json.load(f)
    assert raw.get("layover_roster_wins") is True
