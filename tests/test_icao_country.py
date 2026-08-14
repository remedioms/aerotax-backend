from blueprints.icao_country import country_for_hex


def test_swiss_and_edelweiss_hexes_resolve_to_switzerland():
    # HB-Jxx / HB-Ixx aircraft live in Switzerland's 4B0000–4B7FFF block.
    assert country_for_hex("4b1800") == {
        "iso": "CH", "name": "Schweiz", "flag": "🇨🇭"
    }
    assert country_for_hex("4b7fff")["iso"] == "CH"


def test_adjacent_european_icao_blocks_are_not_shifted():
    assert country_for_hex("4a8000")["iso"] == "SE"
    assert country_for_hex("4b8000")["iso"] == "TR"
    assert country_for_hex("4c0000")["iso"] == "RS"
    assert country_for_hex("4c8000")["iso"] == "CY"
