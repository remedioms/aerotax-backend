from blueprints import family_watch as fw
from flask import Flask


def test_duty_times_are_allowed_but_stay_explicit_opt_in():
    assert "duty_times" in fw.ALLOWED_FIELDS
    assert "duty_times" not in fw.DEFAULT_ALLOWED_FIELDS


def test_family_duty_times_use_canonical_explicit_parsers(monkeypatch):
    helpers = {
        "_rc_briefing_hhmm": lambda day: "06:15" if day.get("marker") else "",
        "_rc_pickup_hhmm": lambda day: "05:30" if day.get("pickup") else "",
    }
    monkeypatch.setattr(fw, "_app_attr",
                        lambda name, default=None: helpers.get(name, default))

    assert fw._family_duty_times({
        "raw_event": {"marker": "06:15 Briefing", "pickup": "05:30"}
    }) == ("06:15", "05:30")


def test_family_duty_times_reject_malformed_values(monkeypatch):
    monkeypatch.setattr(fw, "_app_attr", lambda _name, default=None:
                        (lambda _day: "29:90"))
    assert fw._family_duty_times({"ical_start": "2026-08-07T05:00:00Z"}) == (None, None)


def test_atomic_duty_time_toggle_preserves_other_grant_fields(monkeypatch):
    shares = [{
        "crew_token": "crew-1", "family_token": "family-1",
        "relation": "mama", "fields": ["next_flight", "photos"],
    }]
    monkeypatch.setattr(fw, "_shares_load", lambda: shares)
    monkeypatch.setattr(fw, "_shares_save", lambda value: value is shares)
    app = Flask(__name__)
    with app.test_request_context(json={
        "family_token": "family-1", "relation": "mama",
        "field": "duty_times", "enabled": True,
    }):
        response = fw.family_share_grant("crew-1")

    assert response.get_json()["ok"] is True
    assert shares[0]["fields"] == ["next_flight", "photos", "duty_times"]
