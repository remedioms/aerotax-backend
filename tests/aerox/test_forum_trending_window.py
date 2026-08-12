"""Forum-Feed-Sortierung: „Neu im Forum" + „Heiß diskutiert" (Owner 2026-08-12).

Zwei Owner-Befunde vom Feed-Screenshot:

  1. „Neu im Forum · Frische Threads" zeigte einen 6 Tage alten Thread ganz
     oben, obwohl neuere existierten — der Feed fragte `sort=active` (letzte
     AKTIVITÄT) statt `sort=new` (Erstell-Zeit). Hier ist der Server-Kontrakt
     festgenagelt: `sort=new` sortiert AUSSCHLIESSLICH nach `created_ts` und
     lässt sich von einer frischen Antwort auf einem alten Thread nicht
     hochziehen.

  2. „Heiß diskutiert" stand auf einem festen 7-Tage-Fenster und rotierte
     darum tagelang nicht. Jetzt gestaffelt: 6 h → 24 h → Woche, erstes
     Fenster mit echter Aktivität gewinnt, und das benutzte Fenster kommt als
     `window_hours` zurück (der Client beschriftet die Sektion damit).

Alle Daten-Zugriffe sind gestubbt — der Test prüft NUR Auswahl + Reihenfolge
+ Fenster-Meldung, nicht Supabase.

Run:
    AEROTAX_ALLOW_BOOT_WITHOUT_KEY=1 pytest tests/aerox/test_forum_trending_window.py -v
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

os.environ.setdefault("AEROTAX_ALLOW_BOOT_WITHOUT_KEY", "1")

_THIS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_THIS))
sys.path.insert(0, _REPO)

TOKEN = "AT-TRENDWINDOW-TEST"
HOUR = 3600.0


@pytest.fixture(scope="module")
def appmod():
    import app as _app
    return _app


@pytest.fixture(scope="module")
def client(appmod):
    return appmod.app.test_client()


def _thread(tid, age_h, likes=0, replies=0, last_reply_age_h=None):
    now = time.time()
    return {
        "id": tid,
        "category_id": "general",
        "author_token": "AT-SOMEONE",
        "title": tid,
        "body": "",
        "created_ts": now - age_h * HOUR,
        "like_count": likes,
        "reply_count": replies,
        "last_reply_ts": now - (last_reply_age_h if last_reply_age_h is not None
                                else age_h) * HOUR,
        "hashtags": [],
    }


@pytest.fixture
def stub(appmod, monkeypatch):
    """Alle Daten-Quellen der Threads-Route deterministisch stubben."""
    state = {"threads": [], "activity": {}}

    monkeypatch.setattr(appmod, "_forum_threads_load_recent",
                        lambda limit=500: list(state["threads"]))
    monkeypatch.setattr(appmod, "_forum_threads_load_from_disk", lambda: [])
    monkeypatch.setattr(appmod, "_wall_posts_for_forum", lambda: [])
    monkeypatch.setattr(appmod, "_blocked_by", lambda tok: set())
    monkeypatch.setattr(appmod, "_profile_load", lambda tok: {})
    monkeypatch.setattr(appmod, "_author_avatar_urls", lambda toks: {})
    monkeypatch.setattr(appmod, "_forum_load_likes",
                        lambda tok: {"threads": set(), "replies": set()})

    def _activity_since(cutoff_ts):
        """state['activity'] = {max_alter_h: {thread_id: [kommentare, likes]}}
        → alles, was jünger als der Cutoff ist, zählt."""
        now = time.time()
        span_h = (now - cutoff_ts) / HOUR
        out = {}
        for bucket_h, items in sorted(state["activity"].items()):
            if bucket_h <= span_h + 0.001:
                for tid, pair in items.items():
                    e = out.setdefault(tid, [0, 0])
                    e[0] += pair[0]
                    e[1] += pair[1]
        return out

    monkeypatch.setattr(appmod, "_forum_activity_since", _activity_since)
    return state


def _get(client, sort, limit=50):
    r = client.get(f"/api/forum/{TOKEN}/threads?sort={sort}&limit={limit}")
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    return json.loads(r.get_data(as_text=True))


# ───────────────────────── „Neu im Forum" ─────────────────────────

def test_sort_new_is_creation_time_not_last_activity(client, stub):
    """Der 6-Tage-Thread mit frischer Antwort darf den neuen NICHT verdrängen."""
    stub["threads"] = [
        _thread("alt-aber-aktiv", age_h=144, replies=9, last_reply_age_h=0.2),
        _thread("neu", age_h=1),
        _thread("mittel", age_h=20),
    ]
    ids = [t["id"] for t in _get(client, "new")["threads"]]
    assert ids == ["neu", "mittel", "alt-aber-aktiv"], ids


def test_sort_new_has_no_window_field(client, stub):
    """`window_hours` gehört NUR zu trending — sonst beschriftet der Client
    die falsche Sektion."""
    stub["threads"] = [_thread("a", age_h=1)]
    assert "window_hours" not in _get(client, "new")


def test_sort_active_still_ranks_by_last_activity(client, stub):
    """Regressions-Anker: `active` bleibt, was es war (die Forum-Liste nutzt es)."""
    stub["threads"] = [
        _thread("alt-aber-aktiv", age_h=144, last_reply_age_h=0.2),
        _thread("neu", age_h=1),
    ]
    ids = [t["id"] for t in _get(client, "active")["threads"]]
    assert ids == ["alt-aber-aktiv", "neu"], ids


# ──────────────────────── „Heiß diskutiert" ────────────────────────

def test_trending_prefers_last_6h(client, stub):
    stub["threads"] = [_thread("frisch", age_h=3, replies=2),
                       _thread("gestern", age_h=20, replies=40, likes=99),
                       _thread("woche", age_h=100, replies=80, likes=200)]
    stub["activity"] = {
        2: {"frisch": [2, 1]},
        20: {"gestern": [40, 99]},
        100: {"woche": [80, 200]},
    }
    body = _get(client, "trending")
    assert body["window_hours"] == 6
    assert [t["id"] for t in body["threads"]] == ["frisch"]


def test_trending_widens_to_24h_when_6h_empty(client, stub):
    stub["threads"] = [_thread("gestern-a", age_h=20, replies=3),
                       _thread("gestern-b", age_h=18, replies=1),
                       _thread("woche", age_h=100, replies=80)]
    stub["activity"] = {
        20: {"gestern-a": [1, 0], "gestern-b": [3, 1]},
        100: {"woche": [80, 0]},
    }
    body = _get(client, "trending")
    assert body["window_hours"] == 24
    # Kommentare zählen doppelt: b (3*2+1=7) vor a (1*2=2).
    assert [t["id"] for t in body["threads"]] == ["gestern-b", "gestern-a"]


def test_trending_falls_back_to_week(client, stub):
    stub["threads"] = [_thread("woche", age_h=100, replies=80),
                       _thread("still", age_h=2)]
    stub["activity"] = {100: {"woche": [5, 2]}}
    body = _get(client, "trending")
    assert body["window_hours"] == 168
    assert [t["id"] for t in body["threads"]] == ["woche"]


def test_trending_without_any_activity_is_not_empty(client, stub):
    """Kein Fenster trägt (z. B. Supabase down) → Zähler-Heuristik statt
    leerer Sektion, Fenster meldet ehrlich die Woche."""
    stub["threads"] = [_thread("a", age_h=10, replies=4, likes=2),
                       _thread("b", age_h=12, replies=1)]
    stub["activity"] = {}
    body = _get(client, "trending")
    assert body["window_hours"] == 168
    assert [t["id"] for t in body["threads"]] == ["a", "b"]
