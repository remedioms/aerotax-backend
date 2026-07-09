"""P2-12: inbound_by_route-Suchbox folgt dem GROSSKREIS, nicht dem
Endpunkt-Rechteck — Nordrouten (FRA→HND über Sibirien, Kulmination ~66°N)
lagen sonst ausserhalb der Box. Reine Mathe-Tests, kein fr24-Netz."""
import blueprints.fr24_grpc as G

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
