"""Long FCL.050 imports must release pdfplumber page caches per pair."""

from pathlib import Path


def test_fcl_parser_closes_each_page_pair_after_materializing_rows():
    root = Path(__file__).resolve().parents[1]
    source = (root / 'tools' / 'logbook-parsers' /
              'parse_fcl050_v2.py').read_text(encoding='utf-8')
    sim_loop = source.index('# ── STD-Sektion der B-Seite')
    sort_after_parse = source.index('legs.sort(', sim_loop)
    close_a = source.index('pa.close()', sim_loop, sort_after_parse)
    close_b = source.index('pb.close()', sim_loop, sort_after_parse)
    assert close_a < close_b < sort_after_parse
