"""
Layer-1 Contract Tests · iOS APIClient ↔ Live Prod-Backend
===========================================================

Verifies that every endpoint the iOS `APIClient.swift` consumes returns JSON
whose top-level keys + value types match the iOS `Codable` struct expectations.
Catches the bug class where iOS decoder expects field X but backend sends Y
(z. B. iOS Token-Regex `^AT-[A-Za-z0-9_\\-]+$` vs Backend `AT-HEX`).

Run:
    pytest tests/aerox/test_contract_ios_backend.py -v

The suite hits production:
    BASE = https://api.aerosteuer.de (Hetzner via Cloudflare-Worker)

Cost: ~50 live HTTP calls. Run once per iteration. Tests are intentionally
serial — one shared throwaway account is signed up, all reads/writes use
that token, account is wiped in fixture teardown.

If a test fails: the assertion message contains BOTH the iOS-expected
field-name AND the actual response keys, so the diff is immediately visible.
NO test is skipped / xfail'd — failures are real iOS↔Backend contract bugs.
"""

import os
import re
import time
import uuid
import json
import pytest
import requests


BASE = os.environ.get(
    "AEROX_BACKEND_URL",
    "https://api.aerosteuer.de",
)

# Live-Suite gegen das Produktions-Backend (Signup/Writes über das Netz).
# CI/lokale Läufe dürfen nicht von Prod-Verfügbarkeit abhängen → opt-in wie
# in test_e2e_smoke_live.py via AEROX_LIVE_TESTS=1.
if os.environ.get("AEROX_LIVE_TESTS") != "1":
    pytest.skip(
        "live production contract suite — set AEROX_LIVE_TESTS=1 to run",
        allow_module_level=True,
    )

# iOS Token-Regex aus AuthStore (mirror): ^AT-[A-Za-z0-9_\-]+$
IOS_TOKEN_REGEX = re.compile(r"^AT-[A-Za-z0-9_\-]+$")

TIMEOUT = 30  # seconds — Cloud Run warm, most endpoints <2s


# ---------- helpers ---------------------------------------------------------

# iOS-Client-Kontrakt (BUG-004 Token-Binding, ENFORCE-Modus): der echte
# APIClient sendet auf JEDEM owner-scoped Request `Authorization: Bearer
# <eigenes Token>`. Diese Suite nutzt EINEN Throwaway-Account, dessen Token in
# jedem Pfad (bzw. `?token=`-Query) das EIGENE Token ist — der Bearer wird
# daher zentral aus dem Request-Pfad abgeleitet. Ohne diesen Header lehnen
# die gehärteten Endpunkte (token_binding_required, 401) korrekt ab.
_PATH_TOKEN_RE = re.compile(r"(AT-[A-Za-z0-9_\-]+)")


def _auth_headers(path, extra=None):
    h = dict(extra or {})
    m = _PATH_TOKEN_RE.search(path)
    if m and "Authorization" not in h:
        h["Authorization"] = f"Bearer {m.group(1)}"
    return h


def _post(path, body, *, expect_status=None, bearer=None):
    # `bearer`: für Endpunkte, die das Token im BODY tragen (kein AT-Segment im
    # Pfad — z.B. /api/push/register-apns, /api/user/location). Die gehärteten
    # Handler binden das Body-Token per _request_bearer_matches an den Caller.
    headers = _auth_headers(path)
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    r = requests.post(f"{BASE}/{path.lstrip('/')}", json=body, timeout=TIMEOUT,
                      headers=headers)
    if expect_status is not None:
        assert r.status_code == expect_status, (
            f"POST {path} → {r.status_code} (expected {expect_status}); "
            f"body={r.text[:300]}"
        )
    return r


def _get(path, *, expect_status=None):
    r = requests.get(f"{BASE}/{path.lstrip('/')}", timeout=TIMEOUT,
                     headers=_auth_headers(path))
    if expect_status is not None:
        assert r.status_code == expect_status, (
            f"GET {path} → {r.status_code} (expected {expect_status}); "
            f"body={r.text[:300]}"
        )
    return r


def _put(path, body):
    return requests.put(
        f"{BASE}/{path.lstrip('/')}", json=body, timeout=TIMEOUT,
        headers=_auth_headers(path, {"Content-Type": "application/json"}),
    )


def _delete(path):
    return requests.delete(f"{BASE}/{path.lstrip('/')}", timeout=TIMEOUT,
                           headers=_auth_headers(path))


def _assert_keys(resp_json, expected_keys, *, endpoint, struct_name):
    """Validate top-level keys present in JSON response (case-sensitive)."""
    assert isinstance(resp_json, dict), (
        f"{endpoint}: iOS {struct_name} expects JSON-object, got {type(resp_json).__name__}"
    )
    missing = [k for k in expected_keys if k not in resp_json]
    assert not missing, (
        f"{endpoint}: Backend response missing fields {missing} expected by "
        f"iOS {struct_name}. Got keys: {sorted(resp_json.keys())}"
    )


def _assert_token_format(tok, *, where):
    assert isinstance(tok, str) and tok, f"{where}: token not a non-empty string: {tok!r}"
    assert IOS_TOKEN_REGEX.match(tok), (
        f"{where}: token {tok!r} does NOT match iOS regex {IOS_TOKEN_REGEX.pattern!r} — "
        f"this is the BUG-CLASS the contract test is built to catch."
    )


# ---------- fixture: throwaway account --------------------------------------

@pytest.fixture(scope="session")
def account():
    """Sign up a fresh user. Yield (token, email, password). Teardown deletes."""
    suffix = uuid.uuid4().hex[:12]
    email = f"contract-test-{suffix}@aerox-ci.test"
    password = "AeroX-Contract-Test-2026!"
    r = _post("api/auth/signup", {"email": email, "password": password})
    assert r.status_code == 200, f"signup failed: {r.status_code} {r.text}"
    body = r.json()
    assert body.get("ok") is True, f"signup not ok: {body}"
    token = body.get("token")
    _assert_token_format(token, where="signup")
    yield {"token": token, "email": email, "password": password}
    # Teardown: best-effort delete-account. Passwort MUSS mit (Email/PW-Account):
    # der Token-Pfad allein löscht seit dem P0-Security-Fix keinen PW-Account mehr.
    try:
        _post("api/auth/delete-account", {"token": token, "email": email, "password": password})
    except Exception:
        pass


# ---------- AUTH ------------------------------------------------------------

def test_auth_signup_contract():
    """POST /api/auth/signup — iOS expects AuthResponse{ok, token?, email?, error?}"""
    suffix = uuid.uuid4().hex[:12]
    email = f"contract-signup-{suffix}@aerox-ci.test"
    pw = "AeroX-Smoke-Pass-2026!"
    r = _post("api/auth/signup", {"email": email, "password": pw}, expect_status=200)
    body = r.json()
    # iOS AuthResponse: ok:Bool, token:String?, email:String?, error:String?
    _assert_keys(body, ["ok"], endpoint="POST /api/auth/signup", struct_name="AuthResponse")
    assert isinstance(body["ok"], bool), "AuthResponse.ok must be Bool"
    assert "token" in body, "signup missing 'token' expected by iOS APIClient.signup"
    _assert_token_format(body["token"], where="POST /api/auth/signup")
    # Cleanup (Passwort mit — Token-Pfad allein löscht keinen PW-Account mehr).
    try: _post("api/auth/delete-account", {"token": body["token"], "email": email, "password": pw})
    except Exception: pass


def test_auth_login_contract(account):
    """POST /api/auth/login — AuthResponse with valid AT-token"""
    r = _post("api/auth/login",
              {"email": account["email"], "password": account["password"]},
              expect_status=200)
    body = r.json()
    _assert_keys(body, ["ok", "token", "email"], endpoint="POST /api/auth/login",
                 struct_name="AuthResponse")
    assert body["ok"] is True
    _assert_token_format(body["token"], where="POST /api/auth/login")
    assert body["token"] == account["token"], "login returned different token than signup"


def test_auth_login_invalid_credentials_contract():
    """POST /api/auth/login with bad pw → AuthResponse{ok:false, error:String}"""
    r = _post("api/auth/login",
              {"email": "definitely-not-a-real-user@aerox-ci.test", "password": "wrong"})
    assert r.status_code == 401, f"expected 401 invalid_credentials, got {r.status_code}"
    body = r.json()
    _assert_keys(body, ["ok", "error"], endpoint="POST /api/auth/login (401)",
                 struct_name="AuthResponse")
    assert body["ok"] is False
    assert isinstance(body["error"], str) and body["error"], "error must be non-empty String"


def test_auth_forgot_contract(account):
    """POST /api/auth/forgot — iOS expects AuthResponse decode (ignores response otherwise)"""
    r = _post("api/auth/forgot", {"email": account["email"]})
    assert r.status_code in (200, 400, 404, 429), (
        f"unexpected status {r.status_code}: {r.text[:200]}"
    )
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST /api/auth/forgot",
                     struct_name="AuthResponse")


def test_auth_apple_missing_token_contract():
    """POST /api/auth/apple ohne identity_token → AuthResponse-shape mit error."""
    r = _post("api/auth/apple", {"apple_sub": "x.y.z", "email": "x@y.de"})
    body = r.json()
    _assert_keys(body, ["ok", "error"], endpoint="POST /api/auth/apple",
                 struct_name="AuthResponse")
    assert body["ok"] is False
    assert isinstance(body["error"], str)


# ---------- SESSION + PROFILE ----------------------------------------------

def test_session_contract(account):
    """GET /api/session/<token> — iOS SessionResp{result_data: ResultData?}.

    A brand-new account has no AeroTAX-session yet, so the backend returns 404
    with a state-envelope body. iOS catches that as APIError.http(404) and shows
    the no-result UI. The contract test accepts 200 OR 404; IF 200, the body
    must decode into iOS SessionResp (i.e. contain `result_data`)."""
    r = _get(f"api/session/{account['token']}")
    assert r.status_code in (200, 404), f"unexpected: {r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        body = r.json()
        assert "result_data" in body, (
            f"GET /api/session/<token>: missing 'result_data' expected by iOS "
            f"SessionResp. Got keys: {sorted(body.keys())}"
        )


def test_profile_get_contract(account):
    """GET /api/user/profile/<token> — iOS ProfileResp{profile: UserProfile?}"""
    r = _get(f"api/user/profile/{account['token']}", expect_status=200)
    body = r.json()
    assert "profile" in body, (
        f"GET /api/user/profile: missing 'profile' expected by iOS ProfileResp. "
        f"Got: {sorted(body.keys())}"
    )


def test_profile_put_contract(account):
    """PUT /api/user/profile/<token> — iOS returns AuthResponse-shape."""
    payload = {
        "name": "Contract Tester",
        "homebase": "FRA",
        "position": "Purser",
        "airline": "DLH",
    }
    r = _put(f"api/user/profile/{account['token']}", payload)
    assert r.status_code == 200, f"PUT profile failed: {r.status_code} {r.text[:200]}"
    body = r.json()
    _assert_keys(body, ["ok"], endpoint="PUT /api/user/profile",
                 struct_name="AuthResponse")


def test_stats_contract(account):
    """GET /api/user/stats/<token> — iOS UserStats{totals, top_layovers, z76_by_country, ...}"""
    r = _get(f"api/user/stats/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["totals", "top_layovers", "z76_by_country"],
                 endpoint="GET /api/user/stats", struct_name="UserStats")
    # totals is Codable struct → must be dict with required subkeys
    totals = body["totals"]
    assert isinstance(totals, dict), f"UserStats.totals must be object, got {type(totals)}"
    for sub in ("tour_days", "hotel_nights", "fahr_tage", "arbeitstage",
                "z76_eur", "gesamt"):
        assert sub in totals, (
            f"UserStats.totals missing '{sub}' expected by iOS struct. "
            f"Got: {sorted(totals.keys())}"
        )


def test_trip_stats_contract(account):
    """GET /api/user/trip-stats/<token> — iOS TripStats{has_data, current_year, ...}"""
    r = _get(f"api/user/trip-stats/{account['token']}", expect_status=200)
    body = r.json()
    required = [
        "has_data", "current_year", "lifetime", "ytd",
        "monthly_hours_flown", "monthly_flights",
        "top_destinations", "achievements",
    ]
    _assert_keys(body, required, endpoint="GET /api/user/trip-stats",
                 struct_name="TripStats")
    # iOS TripStats.lifetime + ytd are Bucket structs
    for bucket_key in ("lifetime", "ytd"):
        bk = body[bucket_key]
        assert isinstance(bk, dict), f"TripStats.{bucket_key} must be object"
        for sub in ("hours_flown", "flights", "distance_km",
                    "countries_visited", "countries_list",
                    "frei_days", "sickness_days", "standby_days", "tour_days"):
            assert sub in bk, (
                f"TripStats.{bucket_key} missing '{sub}' for iOS Bucket struct. "
                f"Got: {sorted(bk.keys())}"
            )


def test_subscription_contract(account):
    """GET /api/user/subscription/<token> — iOS Subscription{tier, active, ...}"""
    r = _get(f"api/user/subscription/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["tier", "active"], endpoint="GET /api/user/subscription",
                 struct_name="Subscription")
    assert isinstance(body["tier"], str)
    assert isinstance(body["active"], bool)


def test_subscription_set_contract(account):
    """POST /api/user/subscription/<token>/set — AuthResponse"""
    r = _post(f"api/user/subscription/{account['token']}/set", {"tier": "free"})
    assert r.status_code == 200, f"set sub: {r.status_code} {r.text[:200]}"
    body = r.json()
    _assert_keys(body, ["ok"], endpoint="POST /api/user/subscription/set",
                 struct_name="AuthResponse")


# ---------- FRIENDS + USER-SEARCH ------------------------------------------

def test_friends_contract(account):
    """GET /api/user/friends/<token> — iOS FriendsResp{friends: [Friend]}"""
    r = _get(f"api/user/friends/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["friends"], endpoint="GET /api/user/friends",
                 struct_name="FriendsResp")
    assert isinstance(body["friends"], list)


def test_friend_requests_contract(account):
    """GET /api/user/friend-requests/<token> — iOS FriendRequestsResp{incoming?, outgoing?}"""
    r = _get(f"api/user/friend-requests/{account['token']}", expect_status=200)
    body = r.json()
    # iOS: both are optional, but at least one key should exist
    assert "incoming" in body or "outgoing" in body, (
        f"GET /api/user/friend-requests: response missing both 'incoming' and "
        f"'outgoing' expected by iOS FriendRequestsResp. Got: {sorted(body.keys())}"
    )


def test_overlap_contract(account):
    """GET /api/user/friends/<token>/overlap — iOS OverlapResp{overlaps: [OverlapEntry]}"""
    r = _get(f"api/user/friends/{account['token']}/overlap", expect_status=200)
    body = r.json()
    _assert_keys(body, ["overlaps"], endpoint="GET friends/overlap",
                 struct_name="OverlapResp")
    assert isinstance(body["overlaps"], list)


def test_user_search_contract(account):
    """GET /api/user/search — iOS SearchUsersResp{count?, users?, error?}.

    Backend requires at least one filter (q | airline | homebase) — without it
    returns 400 {users:[], error:'min_query_or_filter_required'}. iOS only ever
    calls this with at least a query, so we test the realistic case."""
    r = _get(f"api/user/search?token={account['token']}&q=test&limit=5",
             expect_status=200)
    body = r.json()
    # users OR error must exist
    assert "users" in body or "error" in body, (
        f"GET /api/user/search: response missing 'users' AND 'error' expected by "
        f"iOS SearchUsersResp. Got: {sorted(body.keys())}"
    )
    if "users" in body and body["users"] is not None:
        assert isinstance(body["users"], list)


def test_friend_roster_self_contract(account):
    """GET /api/user/friend-roster/<token>/<friend> — iOS FriendRosterResp"""
    # call against self · backend should return either days[] or shared:false
    r = _get(f"api/user/friend-roster/{account['token']}/{account['token']}?days=7")
    assert r.status_code in (200, 403, 404), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        # FriendRosterResp{ok?, shared?, reason?, days?}
        assert any(k in body for k in ("ok", "shared", "days", "reason")), (
            f"GET friend-roster: response has none of iOS FriendRosterResp fields. "
            f"Got: {sorted(body.keys())}"
        )


def test_friends_homebases_contract(account):
    """GET /api/user/friends-homebases/<token> — iOS FriendsHomebasesResp{homebases}"""
    r = _get(f"api/user/friends-homebases/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["homebases"], endpoint="GET friends-homebases",
                 struct_name="FriendsHomebasesResp")
    assert isinstance(body["homebases"], list)


def test_friends_today_contract(account):
    """GET /api/user/friends-today/<token> — iOS FriendsTodayResp{datum, friends_today}"""
    r = _get(f"api/user/friends-today/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["datum", "friends_today"],
                 endpoint="GET friends-today", struct_name="FriendsTodayResp")
    assert isinstance(body["friends_today"], list)


def test_friend_groups_contract(account):
    """GET /api/user/friend-groups/<token> — iOS FriendGroupsResp{groups}"""
    r = _get(f"api/user/friend-groups/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["groups"], endpoint="GET friend-groups",
                 struct_name="FriendGroupsResp")
    assert isinstance(body["groups"], list)


# ---------- LUFTHANSA / CALENDAR / ROSTER ----------------------------------

def test_lufthansa_status_contract(account):
    """GET /api/lufthansa/status/<token> — iOS LufthansaStatus{configured, ...}"""
    r = _get(f"api/lufthansa/status/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["configured"], endpoint="GET lufthansa/status",
                 struct_name="LufthansaStatus")
    assert isinstance(body["configured"], bool)


def test_roster_changes_contract(account):
    """GET /api/user/roster-changes/<token> — iOS RosterChangesResp{pending, history}"""
    r = _get(f"api/user/roster-changes/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["pending", "history"], endpoint="GET roster-changes",
                 struct_name="RosterChangesResp")
    assert isinstance(body["pending"], list)
    assert isinstance(body["history"], list)


def test_roster_snapshot_contract(account):
    """POST /api/user/roster-snapshot/<token> — iOS RosterSnapshotResp{ok, changes_count?}"""
    r = _post(f"api/user/roster-snapshot/{account['token']}", {})
    assert r.status_code in (200, 400, 404), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST roster-snapshot",
                     struct_name="RosterSnapshotResp")


# ---------- FLIGHT-OPS + BRIEFING ------------------------------------------

def test_flight_ops_get_all_contract(account):
    """GET /api/user/flight-ops/<token> — iOS FlightOpsResp{ops_by_date}"""
    r = _get(f"api/user/flight-ops/{account['token']}", expect_status=200)
    body = r.json()
    assert "ops_by_date" in body, (
        f"GET flight-ops: missing 'ops_by_date' expected by iOS FlightOpsResp. "
        f"Got: {sorted(body.keys())}"
    )


def test_briefings_get_all_contract(account):
    """GET /api/user/briefing/<token> — iOS BriefingResp{briefings?, count?}"""
    r = _get(f"api/user/briefing/{account['token']}", expect_status=200)
    body = r.json()
    assert "briefings" in body or "count" in body, (
        f"GET briefing: response missing 'briefings' AND 'count' expected by iOS "
        f"BriefingResp. Got: {sorted(body.keys())}"
    )


# ---------- AVIATION MASTER-DATA -------------------------------------------

def test_metar_icao_contract():
    """GET /api/aviation/metar/<ICAO> — iOS MetarResp{icao, reports}"""
    r = _get("api/aviation/metar/EDDF")
    assert r.status_code in (200, 404, 502), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["icao", "reports"], endpoint="GET aviation/metar",
                     struct_name="MetarResp")
        assert isinstance(body["reports"], list)


def test_metar_iata_live_contract():
    """GET /api/weather/metar/<IATA> — iOS MetarReportLive"""
    r = _get("api/weather/metar/FRA")
    assert r.status_code in (200, 404, 502, 400), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        # iOS MetarReportLive: all fields optional, but `status` or `iata` should exist
        assert any(k in body for k in ("status", "iata", "icao", "raw")), (
            f"GET weather/metar: response has none of iOS MetarReportLive fields. "
            f"Got: {sorted(body.keys())}"
        )


def test_currency_contract():
    """GET /api/aviation/currency — iOS CurrencyResp{base, date?, rates}"""
    r = _get("api/aviation/currency?base=EUR&symbols=USD,GBP", expect_status=200)
    body = r.json()
    _assert_keys(body, ["base", "rates"], endpoint="GET aviation/currency",
                 struct_name="CurrencyResp")
    assert isinstance(body["rates"], dict)


def test_aircraft_state_contract():
    """GET /api/aviation/aircraft/<icao24> — iOS AircraftState"""
    r = _get("api/aviation/aircraft/3c6589")  # arbitrary icao24
    assert r.status_code in (200, 404, 502), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        # AircraftState fields are all optional; just verify response is dict
        assert isinstance(body, dict), (
            f"GET aviation/aircraft: iOS AircraftState expects JSON-object. Got: {type(body)}"
        )


def test_notams_contract():
    """GET /api/aviation/notams/<ICAO> — iOS NotamResp{icao, count?, notams}"""
    r = _get("api/aviation/notams/EDDF")
    assert r.status_code in (200, 404, 502), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["icao", "notams"], endpoint="GET aviation/notams",
                     struct_name="NotamResp")
        assert isinstance(body["notams"], list)


# ---------- CREW CHAT -------------------------------------------------------

def test_chat_messages_contract(account):
    """GET /api/crew-chat/<token>/channel/<channel> — iOS ChatMessagesResp{channel, messages}

    Der iOS-Client nutzt NUR `dm__a__b`- und `group__<gid>`-Channels; das
    Membership-Gate (Isolation-Fix) lehnt alles andere — auch das Legacy-
    'general' — mit 400 invalid_channel ab. Wir prüfen die Response-Shape
    daher über den eigenen (leeren) DM-Self-Channel."""
    tok = account["token"]
    dm_self = f"dm__{tok}__{tok}"
    r = _get(f"api/crew-chat/{tok}/channel/{dm_self}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["channel", "messages"],
                 endpoint="GET crew-chat channel", struct_name="ChatMessagesResp")
    assert isinstance(body["messages"], list)

    # Enforce-Coverage des Membership-Gates: unbekanntes Channel-Format → 400.
    r = _get(f"api/crew-chat/{tok}/channel/general")
    assert r.status_code == 400, (
        f"legacy 'general' channel must be rejected (invalid_channel), got {r.status_code}"
    )


def test_dm_mark_read_contract(account):
    """POST /api/crew-chat/<token>/inbox/mark-read — AuthResponse-shape."""
    r = _post(f"api/crew-chat/{account['token']}/inbox/mark-read",
              {"channel_id": "dm:dummy"})
    assert r.status_code in (200, 400, 404), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST chat mark-read",
                     struct_name="AuthResponse")


# ---------- WALL ------------------------------------------------------------

def test_wall_feed_contract(account):
    """GET /api/wall/<token>/feed — iOS WallFeedResp{count?, posts}"""
    r = _get(f"api/wall/{account['token']}/feed?limit=5", expect_status=200)
    body = r.json()
    _assert_keys(body, ["posts"], endpoint="GET wall feed",
                 struct_name="WallFeedResp")
    assert isinstance(body["posts"], list)


def test_wall_create_post_contract(account):
    """POST /api/wall/<token>/post — iOS WallPostResp{ok, post?}"""
    r = _post(f"api/wall/{account['token']}/post",
              {"text": "contract-test-post"})
    assert r.status_code in (200, 400), f"unexpected: {r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST wall post",
                     struct_name="WallPostResp")
        if body.get("ok") and body.get("post"):
            post = body["post"]
            # iOS WallPost expects id, text, etc.
            assert "id" in post, "WallPost missing 'id' expected by iOS"


# ---------- LAYOVER RECS ---------------------------------------------------

def test_layover_recs_iata_contract(account):
    """GET /api/layover-recs/<IATA> — iOS RecsResp{iata?, count?, recs}"""
    r = _get(f"api/layover-recs/JFK?token={account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["recs"], endpoint="GET layover-recs",
                 struct_name="RecsResp")
    assert isinstance(body["recs"], list)


def test_layover_discover_contract(account):
    """GET /api/layover-recs/discover/<token> — iOS DiscoverResp{upcoming_iatas?, recommendations}"""
    r = _get(f"api/layover-recs/discover/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["recommendations"], endpoint="GET layover-recs/discover",
                 struct_name="DiscoverResp")
    assert isinstance(body["recommendations"], list)


def test_layover_aggregate_contract():
    """GET /api/layover-rec/<IATA>/aggregate — iOS LayoverAggregate{iata, avg_stars, total_reviews, breakdown}"""
    r = _get("api/layover-rec/JFK/aggregate", expect_status=200)
    body = r.json()
    _assert_keys(body, ["iata", "avg_stars", "total_reviews", "breakdown"],
                 endpoint="GET layover-rec aggregate",
                 struct_name="LayoverAggregate")
    assert isinstance(body["avg_stars"], (int, float))
    assert isinstance(body["total_reviews"], int)
    assert isinstance(body["breakdown"], dict)


def test_layover_rate_contract(account):
    """POST /api/layover-rec/<IATA>/rate — iOS LayoverRateResp{ok, iata?, category?, stars?}"""
    r = _post("api/layover-rec/JFK/rate",
              {"token": account["token"], "stars": 5, "category": "overall"})
    assert r.status_code in (200, 400, 401), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST layover-rec rate",
                     struct_name="LayoverRateResp")


# ---------- FORUM ----------------------------------------------------------

def test_forum_threads_contract(account):
    """GET /api/forum/<token>/threads — iOS ForumThreadsResp{count?, threads}"""
    r = _get(f"api/forum/{account['token']}/threads?sort=active&limit=10",
             expect_status=200)
    body = r.json()
    _assert_keys(body, ["threads"], endpoint="GET forum threads",
                 struct_name="ForumThreadsResp")
    assert isinstance(body["threads"], list)


def test_forum_trending_contract(account):
    """GET /api/forum/<token>/trending — iOS ForumTrendingResp{count?, tags}"""
    r = _get(f"api/forum/{account['token']}/trending", expect_status=200)
    body = r.json()
    _assert_keys(body, ["tags"], endpoint="GET forum trending",
                 struct_name="ForumTrendingResp")
    assert isinstance(body["tags"], list)


# ---------- MODERATION (Apple 1.4.1) ---------------------------------------

def test_moderation_blocks_contract(account):
    """GET /api/moderation/<token>/blocks — iOS BlocksResp{blocks, count}"""
    r = _get(f"api/moderation/{account['token']}/blocks", expect_status=200)
    body = r.json()
    _assert_keys(body, ["blocks", "count"], endpoint="GET moderation blocks",
                 struct_name="BlocksResp")
    assert isinstance(body["blocks"], list)


def test_moderation_mutes_contract(account):
    """GET /api/moderation/<token>/mutes — iOS MutesResp{mutes}"""
    r = _get(f"api/moderation/{account['token']}/mutes", expect_status=200)
    body = r.json()
    _assert_keys(body, ["mutes"], endpoint="GET moderation mutes",
                 struct_name="MutesResp")
    assert isinstance(body["mutes"], list)


# ---------- PUSH + LOCATION ------------------------------------------------

def test_push_register_contract(account):
    """POST /api/push/register-apns — iOS AuthResponse"""
    r = _post("api/push/register-apns", {
        "token": account["token"],
        "apns_token": "deadbeef" * 8,
        "platform": "ios",
        "bundle_id": "de.aerosteuer.aeris",
    }, bearer=account["token"])
    assert r.status_code in (200, 400), f"unexpected: {r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST push/register-apns",
                     struct_name="AuthResponse")


def test_user_location_contract(account):
    """POST /api/user/location — iOS EmptyResp{ok?}"""
    r = _post("api/user/location",
              {"token": account["token"], "city": "Frankfurt"},
              bearer=account["token"])
    assert r.status_code in (200, 400), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        # iOS EmptyResp only checks ok is optional Bool — accept empty object
        assert isinstance(body, dict)


# ---------- VOICE NOTES ----------------------------------------------------

def test_voice_notes_list_contract(account):
    """GET /api/user/voice-note/<token> — iOS VoiceNoteList{dates, count?}"""
    r = _get(f"api/user/voice-note/{account['token']}", expect_status=200)
    body = r.json()
    _assert_keys(body, ["dates"], endpoint="GET voice-note list",
                 struct_name="VoiceNoteList")
    assert isinstance(body["dates"], list)


# ---------- BUG REPORT + SUPPORT -------------------------------------------

def test_support_message_contract(account):
    """POST /api/support-message — iOS AuthResponse"""
    r = _post("api/support-message", {
        "token": account["token"],
        "message": "contract-test bug-report (auto)",
        "device_info": {"os": "iOS 18.0", "model": "iPhone 15"},
        "source": "shake",
    })
    assert r.status_code in (200, 400), f"unexpected: {r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST support-message",
                     struct_name="AuthResponse")


def test_crash_report_contract():
    """POST /api/crash-report — iOS AuthResponse"""
    r = _post("api/crash-report", {
        "stack": "contract-test crash (auto)",
        "reason": "synthetic",
        "platform": "ios",
    })
    assert r.status_code in (200, 400), f"unexpected: {r.status_code} {r.text[:200]}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["ok"], endpoint="POST crash-report",
                     struct_name="AuthResponse")


# ---------- JOB POLLING ----------------------------------------------------

def test_job_status_404_contract():
    """GET /api/job/<id> for non-existent job → still must return iOS JobStatus-shape or 404."""
    r = _get("api/job/nonexistent-job-id-contract-test")
    assert r.status_code in (200, 404), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        # iOS JobStatus: all optional, but at least one of {state, error, job_id} expected
        assert any(k in body for k in ("state", "error", "job_id")), (
            f"GET /api/job: response has none of iOS JobStatus fields. "
            f"Got: {sorted(body.keys())}"
        )


# ---------- USER LOOKUP BY SHORT ------------------------------------------

def test_lookup_by_short_contract(account):
    """GET /api/user/lookup-by-short/<short> — iOS UserLookupResp{token, name?, ...}.

    Backend returns 404 if no match. We use the first 8 chars of our own token,
    expecting either a 200 hit on ourselves, or 404/409 — all of these MUST be
    handled by iOS APIClient.lookupUserByShort (returns nil on 404/409)."""
    short8 = account["token"][:8]
    r = _get(f"api/user/lookup-by-short/{short8}")
    assert r.status_code in (200, 404, 409), f"unexpected: {r.status_code}"
    if r.status_code == 200:
        body = r.json()
        _assert_keys(body, ["token"], endpoint="GET lookup-by-short",
                     struct_name="UserLookupResp")
        _assert_token_format(body["token"], where="lookup-by-short")
