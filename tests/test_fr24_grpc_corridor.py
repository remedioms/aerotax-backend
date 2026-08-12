"""P2-12: inbound_by_route-Suchbox folgt dem GROSSKREIS, nicht dem
Endpunkt-Rechteck — Nordrouten (FRA→HND über Sibirien, Kulmination ~66°N)
lagen sonst ausserhalb der Box. Reine Mathe-Tests, kein fr24-Netz."""
import blueprints.fr24_grpc as G
import blueprints.aerox_data_blueprint as DATA

FRA = (50.03, 8.57)
HND = (35.55, 139.78)
MUC = (48.35, 11.79)
SFO = (37.62, -122.38)


def _covers(boxes, lat, lon):
    return any(s <= lat <= n and w <= lon <= e for s, n, w, e in boxes)


def _assert_valid(boxes):
    assert boxes
    for s, n, w, e in boxes:
        assert -90.0 <= s < n <= 90.0
        assert -180.0 <= w <= e <= 180.0


def test_fra_hnd_nordroute_kulmination_abgedeckt():
    # Regression: altes Endpunkt-Rechteck kappte bei max(lat)+6 = 56°N.
    boxes = G._corridor_boxes(*FRA, *HND, margin=6.0)
    _assert_valid(boxes)
    assert _covers(boxes, 66.5, 75.0)   # Kulminationspunkt über Sibirien
    assert _covers(boxes, *FRA)
    assert _covers(boxes, *HND)


def test_fra_hnd_wird_in_teilboxen_gesplittet():
    # >120° Lon-Spanne → 3 Teil-Boxen (entschärft fetch-limit-1500-Kappen).
    boxes = G._corridor_boxes(*FRA, *HND, margin=6.0)
    assert len(boxes) == 3


def test_kurzstrecke_bleibt_eine_box():
    boxes = G._corridor_boxes(*FRA, *MUC, margin=6.0)
    _assert_valid(boxes)
    assert len(boxes) == 1
    assert _covers(boxes, *FRA) and _covers(boxes, *MUC)


def test_antimeridian_beide_seiten_abgedeckt():
    boxes = G._corridor_boxes(*HND, *SFO, margin=6.0)
    _assert_valid(boxes)
    assert _covers(boxes, 45.0, 175.0)    # westlich der Datumsgrenze
    assert _covers(boxes, 45.0, -175.0)   # östlich der Datumsgrenze
    assert _covers(boxes, *HND) and _covers(boxes, *SFO)


def test_split_antimeridian_normalisiert():
    # entrollte Lons (>180) → normalisiert + an der Datumsgrenze geteilt
    assert G._split_antimeridian(30.0, 50.0, 170.0, 200.0) == [
        (30.0, 50.0, 170.0, 180.0), (30.0, 50.0, -180.0, -160.0)]
    assert G._split_antimeridian(30.0, 50.0, -10.0, 10.0) == [
        (30.0, 50.0, -10.0, 10.0)]


def test_providers_dedupliziert_stub_provider(monkeypatch):
    # Nicht-verdrahtete Provider = identischer direct-Client → Failover-Schleife
    # darf nicht mehrfach denselben Egress abfragen.
    monkeypatch.setenv("FR24_GRPC_PROVIDERS", "direct,cloudflare,nas")
    assert G._providers() == ["direct"]
    monkeypatch.setenv("FR24_GRPC_PROVIDERS", "cloudflare,nas")
    assert G._providers() == ["cloudflare"]


# ── detail_card: FR24s ECHTE Ist-Ankunft nicht mehr wegwerfen (2026-08-13) ──
# Die Karte trug `actual_dep`, ließ `actual_arrival` aus DERSELBEN Antwort aber
# fallen. Der Wert kostet nichts extra (kein neuer Call, HARD-CACHE-Regel
# unberührt) — weggeworfen wurde er nur, weil ihn niemand abholte.

def _stub_detail(monkeypatch, schedule_info):
    monkeypatch.setattr(G, 'tap_detail', lambda **k: {
        'row': {'extra_info': {'reg': 'D-AIHY', 'flight': 'LH400'}},
        'detail': {'schedule_info': schedule_info,
                   'aircraft_info': {'reg': 'D-AIHY', 'type': 'A346'}},
    })


def test_detail_card_reicht_ist_ankunft_durch(monkeypatch):
    _stub_detail(monkeypatch, {'flight_number': 'LH400',
                               'scheduled_departure': 1_783_580_000,
                               'actual_departure': 1_783_580_600,
                               'scheduled_arrival': 1_783_600_000,
                               'actual_arrival': 1_783_599_100})
    card = G.detail_card(callsign='DLH400', lat=50.0, lon=8.5)
    assert card['actual_dep'] == 1_783_580_600
    assert card['actual_arr'] == 1_783_599_100


def test_detail_card_ohne_ist_ankunft_bleibt_ohne_feld(monkeypatch):
    """Fliegender Flug: FR24 kennt die Landung noch nicht — dann steht dort
    auch nichts (None-Felder fallen aus der Karte)."""
    _stub_detail(monkeypatch, {'flight_number': 'LH400',
                               'scheduled_departure': 1_783_580_000,
                               'actual_departure': 1_783_580_600,
                               'scheduled_arrival': 1_783_600_000})
    card = G.detail_card(callsign='DLH400', lat=50.0, lon=8.5)
    assert 'actual_arr' not in card
    assert card['actual_dep'] == 1_783_580_600


def test_detail_card_by_flightid_uses_exact_instance_without_livefeed(monkeypatch):
    """LH732-Repro: eine alte Position kann ausserhalb der kleinen Tap-Box
    liegen. Die bereits bekannte Flight-ID lädt die ETA ohne räumliches Raten."""
    async def _exact(_provider, fid):
        assert fid == 987654321
        return {'row': {}, 'detail': {
            'schedule_info': {
                'flight_number': 'LH732',
                'scheduled_arrival': 1_786_591_500,
            },
            'flight_progress': {
                'eta': 1_786_589_160,
                'flight_stage': 'AIRBORNE',
            },
        }}

    monkeypatch.setattr(G, 'available', lambda: True)
    monkeypatch.setattr(G, '_allow_call', lambda: True)
    monkeypatch.setattr(G, '_providers', lambda: ['direct'])
    monkeypatch.setattr(G, '_detail_by_fid_async', _exact)

    card = G.detail_card_by_flightid(987654321)

    assert card['flight_number'] == 'LH732'
    assert card['eta'] == 1_786_589_160
    assert card['flight_stage'] == 'AIRBORNE'


def test_shared_live_card_prefers_exact_flightid(monkeypatch):
    exact = {
        'route_from': 'FRA', 'route_to': 'PVG',
        'reg': 'D-AIXF', 'eta': 1_786_589_160,
    }
    calls = []
    monkeypatch.setattr(G, 'detail_card_by_flightid',
                        lambda fid: calls.append(('id', fid)) or dict(exact))
    monkeypatch.setattr(G, 'detail_card',
                        lambda **kw: calls.append(('geo', kw)) or None)
    DATA._FR24_LIVE_CARD_MEMO.clear()

    card = DATA._fr24_live_card_cached(
        flight_no='LH732', callsign='DLH732', reg='D-AIXF',
        lat=42.56, lon=32.23, origin='FRA', dest='PVG',
        flightid=987654321)

    assert card['eta'] == 1_786_589_160
    assert calls == [('id', 987654321)]


def test_shared_live_card_falls_back_to_geo_after_exact_id_miss(monkeypatch):
    calls = []
    monkeypatch.setattr(G, 'detail_card_by_flightid',
                        lambda fid: calls.append(('id', fid)) or None)
    monkeypatch.setattr(G, 'detail_card',
                        lambda **kw: calls.append(('geo', kw)) or {
                            'route_from': 'FRA', 'route_to': 'PVG',
                            'reg': 'D-AIXF', 'eta': 1_786_589_160,
                        })
    DATA._FR24_LIVE_CARD_MEMO.clear()

    card = DATA._fr24_live_card_cached(
        flight_no='LH732', callsign='DLH732', reg='D-AIXF',
        lat=42.56, lon=32.23, origin='FRA', dest='PVG',
        flightid=987654321)

    assert card['eta'] == 1_786_589_160
    assert [kind for kind, _ in calls] == ['id', 'geo']
