"""Focused add/read/discover contracts for crew layover recommendations.

The write endpoint must never acknowledge a recommendation which the durable
read path cannot subsequently return. Owner identity is persisted internally,
but must not be exposed by any public response.
"""
from datetime import date

import pytest

import app


TOKEN = "AT-layover-owner-123456"


@pytest.fixture
def client(monkeypatch):
    app.app.config.update(TESTING=True)
    monkeypatch.setattr(
        app, "_validate_token",
        lambda _token: app._TokenValidationResult(
            app._TokenValidationState.VALID, "owner@example.test"
        ),
    )
    return app.app.test_client()


def _stub_add_dependencies(monkeypatch, *, sb_available=True, sb_ok=True,
                           disk_ok=True):
    captured = []
    monkeypatch.setattr(app, "SB_AVAILABLE", sb_available)
    monkeypatch.setattr(app, "_profile_load", lambda _t: {
        "profile": {"name": "Basti", "airline": "Swiss"}
    })
    monkeypatch.setattr(app, "_recs_load_from_disk", lambda _iata: [])
    monkeypatch.setattr(
        app, "_layover_recs_save_to_supabase",
        lambda rows: captured.extend(rows) or sb_ok,
    )
    monkeypatch.setattr(app, "_recs_save_disk", lambda _iata, _rows: disk_ok)
    monkeypatch.setattr(app, "_votes_load", lambda _token: {})
    monkeypatch.setattr(app, "_layover_vote_sb_set", lambda *_args: True)
    monkeypatch.setattr(app, "_votes_save_disk", lambda *_args: None)
    return captured


def _post(client, **overrides):
    body = {
        "iata": "SFO",
        "category": "other",
        "title": "Bay cruise after pickup",
        "description": "Short crew-tested trip",
    }
    body.update(overrides)
    return client.post(
        f"/api/layover-recs/{TOKEN}/add",
        json=body,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )


def test_add_waits_for_durable_supabase_ack(client, monkeypatch):
    disk_called = []
    _stub_add_dependencies(monkeypatch, sb_available=True, sb_ok=False)
    monkeypatch.setattr(
        app, "_recs_save_disk",
        lambda *_args: disk_called.append(True) or True,
    )

    response = _post(client)

    assert response.status_code == 503
    assert response.get_json()["error"] == "persist_failed"
    assert disk_called == []  # no cache-only ghost which the SB read omits


def test_add_requires_disk_ack_when_supabase_unavailable(client, monkeypatch):
    _stub_add_dependencies(
        monkeypatch, sb_available=False, sb_ok=False, disk_ok=False
    )

    response = _post(client)

    assert response.status_code == 503
    assert response.get_json()["ok"] is False


def test_add_persists_exact_owner_but_never_exposes_token(client, monkeypatch):
    captured = _stub_add_dependencies(monkeypatch)

    response = _post(client)

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert captured[0]["author_token"] == TOKEN
    assert captured[0]["author_name"] == "Basti"
    assert "author_token" not in body["rec"]


def test_anonymous_add_keeps_owner_internal_and_profile_private(client, monkeypatch):
    captured = _stub_add_dependencies(monkeypatch)

    response = _post(client, is_anonymous=True)

    assert response.status_code == 200
    stored = captured[0]
    assert stored["author_token"] == TOKEN
    assert "author_short" not in stored
    assert "author_name" not in stored
    # Kept internally so anonymous sleep tips cannot bypass crew-hotel privacy.
    assert stored["author_airline"] == "Swiss"
    public = response.get_json()["rec"]
    assert "author_token" not in public
    assert "author_short" not in public
    assert "author_airline" not in public


@pytest.mark.parametrize("category", ["adventure", "Food ", "..."])
def test_add_rejects_unknown_category_instead_of_silently_reclassifying(
        client, monkeypatch, category):
    saved = _stub_add_dependencies(monkeypatch)

    response = _post(client, category=category)

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_category"
    assert saved == []


def test_get_rejects_unknown_category_before_loading_storage(client, monkeypatch):
    monkeypatch.setattr(app, "_recs_path", lambda _iata: "/valid")
    monkeypatch.setattr(
        app, "_recs_load",
        lambda _iata: pytest.fail("invalid filter must not query storage"),
    )

    response = client.get("/api/layover-recs/SFO?category=adventure")

    assert response.status_code == 400
    assert response.get_json()["error"] == "invalid_category"


def test_owner_sees_own_sleep_tip_without_calendar_and_token_stays_private(
        client, monkeypatch):
    rec = {
        "id": "own-sleep",
        "iata": "SFO",
        "category": "sleep",
        "title": "Quiet hotel",
        "author_token": TOKEN,
        "author_airline": "Swiss",
        "vote_score": 1,
    }
    monkeypatch.setattr(app, "_recs_path", lambda _iata: "/valid")
    monkeypatch.setattr(app, "_recs_load", lambda _iata: [rec])
    monkeypatch.setattr(app, "_viewer_airline_and_calendar", lambda _t: ("", False))
    monkeypatch.setattr(app, "_votes_load", lambda _t: {})

    response = client.get(f"/api/layover-recs/SFO?token={TOKEN}")

    assert response.status_code == 200
    public = response.get_json()["recs"]
    assert [r["id"] for r in public] == ["own-sleep"]
    assert "author_token" not in public[0]


def test_discover_uses_supabase_read_path_and_redacts_owner_token(
        client, monkeypatch):
    rec = {
        "id": "fresh",
        "iata": "SFO",
        "category": "other",
        "title": "Bay cruise",
        "author_token": TOKEN,
        "vote_score": 1,
    }
    monkeypatch.setitem(app._store, TOKEN, {
        "result_data": {"_tage_detail": [{
            "datum": date.today().isoformat(),
            "reader_facts": {"layover_ort": "SFO"},
        }]}
    })
    monkeypatch.setattr(app, "_recs_path", lambda _iata: "/valid")
    monkeypatch.setattr(app, "_recs_load", lambda _iata: [rec])
    monkeypatch.setattr(app, "_viewer_airline_and_calendar", lambda _t: ("", False))

    response = client.get(f"/api/layover-recs/discover/{TOKEN}")

    assert response.status_code == 200
    top = response.get_json()["recommendations"][0]["top_recs"][0]
    assert top["id"] == "fresh"
    assert "author_token" not in top
