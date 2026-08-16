import time
import urllib.request

import app as backend


def test_metar_failure_serves_stale_and_negative_caches(monkeypatch):
    backend._AVIATION_CACHE.clear()
    backend._AVIATION_CACHE["metar_v2:EDDF"] = (
        time.time() - 1,
        {"iata": "FRA", "icao": "EDDF", "status": "ok", "temp_c": 18},
    )
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("upstream stalled")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    client = backend.app.test_client()
    first = client.get("/api/weather/metar/FRA")
    second = client.get("/api/weather/metar/FRA")

    assert first.status_code == second.status_code == 200
    assert first.get_json()["stale"] is True
    assert second.get_json()["stale"] is True
    assert len(calls) == 1


def test_metar_failure_without_stale_is_terminal_for_short_window(monkeypatch):
    backend._AVIATION_CACHE.clear()
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        raise TimeoutError("upstream stalled")

    monkeypatch.setattr(urllib.request, "urlopen", fail)
    client = backend.app.test_client()
    first = client.get("/api/weather/metar/FRA")
    second = client.get("/api/weather/metar/FRA")

    assert first.status_code == second.status_code == 200
    assert first.get_json()["status"] == "fetch_failed"
    assert second.get_json()["status"] == "fetch_failed"
    assert len(calls) == 1
