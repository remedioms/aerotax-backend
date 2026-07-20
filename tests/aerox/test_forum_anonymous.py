"""Forum „Anonym posten" (Owner 2026-07-20) — Local-Smoke via test_client.

Verifiziert die Anonym-Option für Forum-Threads + -Replies (Wall-Muster:
`is_anonymous` unterdrückt den Author-Snapshot, per-Item-gesalzener
`anon_handle` via `_anon_handle_for`, `author_token` bleibt NUR server-seitig
für Ownership/Moderation und wird nie ausgeliefert):

  - anonymer Thread: Create-Response + Threads-Liste ohne Profil-Reste
    (author_name/role/airline/homebase/avatar/short), anon_handle gesetzt,
    is_mine bleibt für den Autor true (Löschen weiter möglich), Betrachter
    sehen is_mine=false und ebenfalls keine Author-Felder.
  - anonymer Reply: dito in Create-Response + Replies-Liste.
  - anonym ⇒ scope wird auf 'all' erzwungen (airline-only wäre unsichtbar
    UND ein Airline-Identitäts-Hinweis).
  - Ownership: Autor kann den eigenen anonymen Thread/Reply löschen.
  - Nicht-anonym bleibt unverändert (Author-Snapshot aus dem Profil).
  - wall:-Brücke: anonyme Wall-Posts werden weiterhin NICHT ins Forum
    gespiegelt (Regression-Guard, Verhalten unverändert).

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/aerox/test_forum_anonymous.py -v
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

_THIS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_THIS))
sys.path.insert(0, _REPO)


# Profil-Felder die bei anonymen Einträgen NIE (mit Inhalt) ausgeliefert
# werden dürfen — analog der get_comments-Säuberung der Wall.
AUTHOR_FIELDS = ("author_name", "author_role", "author_airline",
                 "author_homebase", "author_avatar", "author_short")


@pytest.fixture(scope="module")
def client():
    import app as _app
    return _app.app.test_client()


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _make_user(client, name):
    """Throwaway-User via Signup + gesetztem Profil (damit ein Leak sichtbar
    WÄRE — ohne Profilnamen gäbe es nichts zu leaken)."""
    email = f"forumanon+{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}@aerox.test"
    pw = "Test12345!"
    r = client.post("/api/auth/signup", json={"email": email, "password": pw})
    assert r.status_code == 200, f"signup {r.status_code}: {r.get_data(as_text=True)[:200]}"
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True
    token = body["token"]
    r = client.put(f"/api/user/profile/{token}", json={
        "name": name, "homebase": "FRA", "position": "Purser", "airline": "DLH",
    }, headers=_bearer(token))
    assert r.status_code == 200, r.get_data(as_text=True)[:200]
    return {"email": email, "password": pw, "token": token, "name": name}


def _cleanup_user(client, u):
    try:
        client.post("/api/auth/delete-account",
                    json={"email": u["email"], "password": u["password"],
                          "token": u["token"]})
    except Exception:
        pass


@pytest.fixture
def author(client):
    u = _make_user(client, "Maria Musterfrau")
    yield u
    _cleanup_user(client, u)


@pytest.fixture
def viewer(client):
    u = _make_user(client, "Victor Viewer")
    yield u
    _cleanup_user(client, u)


def _assert_no_author_leak(item, ctx):
    """Kein Profil-Rest + kein author_token auf einem anonymen Item."""
    assert "author_token" not in item, f"{ctx}: author_token geleakt: {item!r}"
    for f in AUTHOR_FIELDS:
        assert not item.get(f), (
            f"{ctx}: {f}={item.get(f)!r} darf bei anonym nicht ausgeliefert "
            f"werden. Item: {item!r}"
        )
    assert item.get("is_anonymous") is True, f"{ctx}: is_anonymous fehlt: {item!r}"
    assert item.get("anon_handle"), f"{ctx}: anon_handle fehlt: {item!r}"
    # Der Handle darf offensichtlich kein Profil-/Token-Derivat sein.
    assert "Maria" not in str(item.get("anon_handle"))
    assert not str(item.get("anon_handle")).startswith("AT-")


def _create_thread(client, token, is_anonymous=False, scope="all",
                   title="Anon-Test-Thread"):
    r = client.post(f"/api/forum/{token}/threads", json={
        "category_id": "general", "title": title,
        "body": f"Testbody {uuid.uuid4().hex[:6]}",
        "is_anonymous": is_anonymous, "scope": scope,
    }, headers=_bearer(token))
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True
    return body["thread"]


def _list_threads(client, token):
    r = client.get(f"/api/forum/{token}/threads?category=general&limit=200")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return json.loads(r.get_data(as_text=True)).get("threads") or []


# ─────────────────────────────────────────────────────────────────
# Anonymer Thread
# ─────────────────────────────────────────────────────────────────

def test_anonymous_thread_no_author_leak_in_create_and_list(client, author, viewer):
    t = _create_thread(client, author["token"], is_anonymous=True)
    _assert_no_author_leak(t, "create-response")

    # Liste aus Autor-Sicht: kein Author-Leak, aber Ownership (is_mine) bleibt —
    # sonst könnte der Autor seinen eigenen anonymen Thread nicht mehr löschen.
    mine = next((x for x in _list_threads(client, author["token"])
                 if x.get("id") == t["id"]), None)
    assert mine is not None, "anonymer Thread fehlt in der Liste des Autors"
    _assert_no_author_leak(mine, "list(author)")
    assert mine.get("is_mine") is True

    # Liste aus Fremd-Sicht: ebenfalls kein Leak, is_mine false.
    other = next((x for x in _list_threads(client, viewer["token"])
                  if x.get("id") == t["id"]), None)
    assert other is not None, "anonymer Thread fehlt in der Liste des Viewers"
    _assert_no_author_leak(other, "list(viewer)")
    assert other.get("is_mine") is False


def test_anonymous_thread_forces_scope_all(client, author):
    t = _create_thread(client, author["token"], is_anonymous=True, scope="airline")
    assert t.get("scope") == "all", (
        f"anonym + airline-only muss auf scope=all erzwungen werden, got {t.get('scope')!r}"
    )


def test_anonymous_thread_owner_can_delete(client, author):
    t = _create_thread(client, author["token"], is_anonymous=True)
    r = client.delete(f"/api/forum/{author['token']}/threads/{t['id']}",
                      headers=_bearer(author["token"]))
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert json.loads(r.get_data(as_text=True)).get("ok") is True


def test_non_anonymous_thread_keeps_author_snapshot(client, author):
    t = _create_thread(client, author["token"], is_anonymous=False)
    assert t.get("author_name") == author["name"], (
        f"Nicht-anonym muss den Profil-Snapshot behalten: {t!r}"
    )
    assert t.get("author_short"), "author_short fehlt bei nicht-anonym"
    assert not t.get("anon_handle"), "anon_handle darf bei nicht-anonym nicht gesetzt sein"


# ─────────────────────────────────────────────────────────────────
# Anonymer Reply
# ─────────────────────────────────────────────────────────────────

def test_anonymous_reply_no_author_leak(client, author, viewer):
    # Thread NICHT anonym (vom Viewer), Antwort anonym (vom Autor).
    t = _create_thread(client, viewer["token"], title="Reply-Host")
    r = client.post(
        f"/api/forum/{author['token']}/threads/{t['id']}/reply",
        json={"body": "Anonyme Antwort", "is_anonymous": True},
        headers=_bearer(author["token"]))
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = json.loads(r.get_data(as_text=True))
    assert body.get("ok") is True
    _assert_no_author_leak(body["reply"], "reply-create-response")
    reply_id = body["reply"]["id"]

    def _replies(viewer_token):
        rr = client.get(f"/api/forum/{viewer_token}/threads/{t['id']}/replies")
        assert rr.status_code == 200, rr.get_data(as_text=True)[:300]
        return json.loads(rr.get_data(as_text=True)).get("replies") or []

    mine = next((x for x in _replies(author["token"]) if x.get("id") == reply_id), None)
    assert mine is not None, "anonymer Reply fehlt in der Replies-Liste (Autor)"
    _assert_no_author_leak(mine, "replies(author)")
    assert mine.get("is_mine") is True

    other = next((x for x in _replies(viewer["token"]) if x.get("id") == reply_id), None)
    assert other is not None, "anonymer Reply fehlt in der Replies-Liste (Viewer)"
    _assert_no_author_leak(other, "replies(viewer)")
    assert other.get("is_mine") is False

    # Ownership: Autor löscht den eigenen anonymen Reply (author_token bleibt
    # server-seitig erhalten, wird aber nie ausgeliefert).
    r = client.delete(f"/api/forum/{author['token']}/replies/{reply_id}",
                      headers=_bearer(author["token"]))
    assert r.status_code == 200, r.get_data(as_text=True)[:300]


def test_non_anonymous_reply_keeps_author_snapshot(client, author, viewer):
    t = _create_thread(client, viewer["token"], title="Reply-Host-2")
    r = client.post(
        f"/api/forum/{author['token']}/threads/{t['id']}/reply",
        json={"body": "Normale Antwort"},
        headers=_bearer(author["token"]))
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    reply = json.loads(r.get_data(as_text=True))["reply"]
    assert reply.get("author_name") == author["name"]
    assert not reply.get("anon_handle")


# ─────────────────────────────────────────────────────────────────
# wall:-Brücke — anonyme Wall-Posts bleiben draußen (unverändert)
# ─────────────────────────────────────────────────────────────────

def test_wall_anonymous_post_not_bridged_into_forum(client, author):
    r = client.post(f"/api/wall/{author['token']}/post", json={
        "text": "Anonymer Feed-Post mit Kategorie\n\n#general",
        "is_anonymous": True,
    }, headers=_bearer(author["token"]))
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    post = json.loads(r.get_data(as_text=True)).get("post") or {}
    post_id = post.get("id")
    assert post_id, "Wall-Post-Create lieferte keine id"

    ids = {x.get("id") for x in _list_threads(client, author["token"])}
    assert f"wall:{post_id}" not in ids, (
        "Anonymer Wall-Post darf NICHT ins Forum gespiegelt werden "
        "(wall:-Brücke muss anonyme Posts weiter ausblenden)."
    )
