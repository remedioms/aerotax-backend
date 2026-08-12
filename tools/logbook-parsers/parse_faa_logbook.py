#!/usr/bin/env python3
"""OffBlock ``FAA Logbook Pro`` PDF -> AeroX logbook JSON.

The export consists of alternating A/B pages. A contains date, aircraft,
routing, duration and landings; B contains night time, simulator time and
pilot function. The parser is deliberately tied to the verified table layout
and reconciles flight time, simulator time and landings against the document's
first ``AMOUNT FORWARDED`` and final ``TOTALS TO DATE`` rows.
"""
import json
import os
import re
import sys

import pdfplumber

from parse_fcl050_v2 import bands, cells, iso_date, to_int, to_min


A_COLS = {
    'date': (20, 83), 'type': (83, 140), 'reg': (140, 196),
    'from': (196, 241), 'to': (241, 285), 'total': (285, 337),
    'ldg_day': (677, 706), 'ldg_night': (706, 738),
}
B_COLS = {
    'night': (47, 93), 'actual_instr': (93, 136),
    'hood': (136, 181), 'app': (181, 224),
    'approach': (224, 267), 'sim': (267, 310),
    'cross': (310, 354), 'solo': (354, 386),
    'pic': (386, 425), 'sic': (425, 468),
    'dual': (468, 509), 'instr': (509, 550),
    'remarks': (550, 770),
}
RE_DATE = re.compile(r'^\d{2}/\d{2}/\d{2}$')


def matches_pdf(src):
    """True only for the verified OffBlock FAA Logbook Pro table."""
    try:
        with pdfplumber.open(src) as pdf:
            if len(pdf.pages) < 3:
                return False
            cover = re.sub(
                r'\s+', '', (pdf.pages[0].extract_text() or '').upper())
            page = pdf.pages[1]
            words = page.extract_words()
            squeezed = re.sub(
                r'\s+', '', (page.extract_text() or '').upper())
            has_date = any(
                A_COLS['date'][0] <= word['x0'] < A_COLS['date'][1]
                and RE_DATE.fullmatch(word.get('text') or '')
                for word in words)
            return ('PILOTLOGBOOKFAALOGBOOKPRO' in cover
                    and has_date and len(bands(page)) >= 5
                    and 'ROUTEOFFLIGHT' in squeezed
                    and 'TOTALDURATION' in squeezed
                    and 'LANDINGS' in squeezed)
    except Exception:
        return False


def _normalize_types(legs):
    present = {leg.get('type') for leg in legs if leg.get('type')}
    renamed = {}
    for leg in legs:
        aircraft_type = leg.get('type') or ''
        if aircraft_type.isdigit() and 'A' + aircraft_type in present:
            renamed[aircraft_type] = 'A' + aircraft_type
            leg['type'] = 'A' + aircraft_type
    return renamed


def parse_pdf(src):
    legs, sims, totals = [], [], []
    carry_rows_min = 0

    with pdfplumber.open(src) as pdf:
        for index in range(1, len(pdf.pages) - 1, 2):
            page_a, page_b = pdf.pages[index], pdf.pages[index + 1]
            words_a, words_b = page_a.extract_words(), page_b.extract_words()
            bands_a, bands_b = bands(page_a), bands(page_b)
            if len(bands_a) < 5 or len(bands_b) < 5:
                raise ValueError('FAA Logbook Pro table bands missing')

            # A and B are printed from the same row grid. Use the A bands for
            # paired flight rows; the verified exports align them exactly.
            for row in range(len(bands_a) - 1):
                top, bottom = bands_a[row], bands_a[row + 1]
                if bottom - top < 8:
                    continue
                values_a = cells(words_a, top, bottom, A_COLS)
                date = iso_date(values_a['date'])
                values_b = cells(words_b, top, bottom, B_COLS)
                if not date:
                    joined = ' '.join(values_a.values()).upper()
                    # OffBlock's FAA template spells the label
                    # "AMOUNT FOREWARDED" (sic). Accept only that observed
                    # label, plus the correctly spelled variant defensively.
                    if (('AMOUNT FORWARDED' in joined
                         or 'AMOUNT FOREWARDED' in joined)
                            or 'TOTALS TO DATE' in joined):
                        totals.append((page_a.page_number, joined,
                                       values_a, values_b))
                    continue

                block = to_min(values_a['total'])
                sim_min = to_min(values_b['sim'])
                landings = (to_int(values_a['ldg_day'])
                            + to_int(values_a['ldg_night']))
                aircraft_type = values_a['type'].replace(' ', '').upper()
                registration = values_a['reg'].replace(' ', '').upper()
                origin = values_a['from'].strip().upper()
                destination = values_a['to'].strip().upper()

                if block and block >= 1200:
                    if (origin == destination and not aircraft_type
                            and not registration and landings == 0
                            and not sim_min):
                        carry_rows_min += block
                        continue
                    raise ValueError(
                        f'unplausible >20h FAA row {date} '
                        f'{origin}→{destination}: {values_a["total"]}')

                # FAA prints FSTD sessions on an otherwise empty same-place A
                # row; the duration is only in B's Flight Simulator column.
                if sim_min:
                    if origin != destination or block:
                        raise ValueError(
                            f'ambiguous FAA simulator row {date} '
                            f'{origin}→{destination}')
                    sim = {'date': date, 'duration_min': sim_min,
                           'code': aircraft_type or 'FSTD'}
                    sims.append(sim)
                    # One verified training row also carries three landings.
                    # FAA includes them in its landing total, while the FSTD
                    # duration remains separate from flight duration. Preserve
                    # both facts as a landing-only leg plus a sim session.
                    if landings:
                        leg = {'date': date, 'from': origin,
                               'to': destination,
                               '_source_format': 'offblock_faa'}
                        for key in ('ldg_day', 'ldg_night'):
                            value = to_int(values_a[key])
                            if value:
                                leg[key] = value
                        for key, role in (('pic', 'PIC'), ('sic', 'FO'),
                                          ('dual', 'DUAL'), ('instr', 'FI')):
                            if to_min(values_b[key]):
                                leg['role'] = role
                                break
                        legs.append(leg)
                    continue
                if not block:
                    # Empty accounting rows contain no flight time and are not
                    # logbook legs. Any landing on such a row is contradictory.
                    if landings:
                        raise ValueError(
                            f'FAA landing without flight duration on {date}')
                    continue
                if not origin or not destination:
                    raise ValueError(f'FAA route missing on {date}')

                leg = {
                    'date': date, 'from': origin, 'to': destination,
                    'block_min': block, '_source_format': 'offblock_faa',
                }
                if aircraft_type:
                    leg['type'] = aircraft_type
                if registration:
                    leg['reg'] = registration
                for key in ('ldg_day', 'ldg_night'):
                    value = to_int(values_a[key])
                    if value:
                        leg[key] = value
                night = to_min(values_b['night'])
                if night:
                    leg['night_min'] = night
                for key, role in (('pic', 'PIC'), ('sic', 'FO'),
                                  ('dual', 'DUAL'), ('instr', 'FI')):
                    if to_min(values_b[key]):
                        leg['role'] = role
                        break
                remarks = values_b['remarks'].strip()
                if remarks:
                    leg['remarks'] = remarks[:500]
                legs.append(leg)

            page_a.close()
            page_b.close()

    legs.sort(key=lambda leg: (leg['date'], leg.get('from') or '',
                               leg.get('to') or ''))
    sims.sort(key=lambda sim: sim['date'])
    renamed = _normalize_types(legs)
    from legkeys import dedupe_keys
    collisions = dedupe_keys(legs)

    previous = [row for row in totals
                if ('AMOUNT FORWARDED' in row[1]
                    or 'AMOUNT FOREWARDED' in row[1])]
    final = [row for row in totals if 'TOTALS TO DATE' in row[1]]
    if not previous or not final:
        raise ValueError('FAA Logbook Pro totals or amount forwarded missing')
    first_page, _, opening_a, opening_b = previous[0]
    final_page, _, final_a, final_b = final[-1]
    opening_total = to_min(opening_a['total']) or 0
    carryover_min = opening_total + carry_rows_min
    final_total = to_min(final_a['total'])
    parsed_block = sum(leg.get('block_min', 0) for leg in legs)
    parsed_landings = sum(
        leg.get('ldg_day', 0) + leg.get('ldg_night', 0) for leg in legs)
    opening_landings = (to_int(opening_a['ldg_day'])
                        + to_int(opening_a['ldg_night']))
    final_landings = (to_int(final_a['ldg_day'])
                      + to_int(final_a['ldg_night']))
    parsed_sim = sum(sim['duration_min'] for sim in sims)
    opening_sim = to_min(opening_b['sim']) or 0
    final_sim = to_min(final_b['sim'])
    errors = []
    expected_block = (final_total - carryover_min
                      if final_total is not None else None)
    if parsed_block != expected_block:
        errors.append(f'Block {parsed_block} != PDF delta {expected_block} min')
    expected_landings = final_landings - opening_landings
    if parsed_landings != expected_landings:
        errors.append(
            f'Landings {parsed_landings} != PDF delta {expected_landings}')
    expected_sim = (final_sim - opening_sim
                    if final_sim is not None else None)
    if parsed_sim != expected_sim:
        errors.append(f'Simulator {parsed_sim} != PDF delta {expected_sim} min')
    if errors:
        raise ValueError(f'{os.path.basename(src)}: ' + '; '.join(errors))

    return legs, sims, {
        'filename': os.path.basename(src),
        'month': f'{legs[0]["date"][:7]}–{legs[-1]["date"][:7]}',
        'legs': len(legs), 'sim_sessions': len(sims),
        'block_min': parsed_block, 'sim_min': parsed_sim,
        'landings': parsed_landings, 'carryover_min': carryover_min,
        'opening_total_min': opening_total,
        'carry_rows_min': carry_rows_min,
        'opening_page': first_page, 'final_page': final_page,
        'renamed_types': renamed, 'collisions': collisions,
        'control': 'OK',
    }


def main():
    src, dst = sys.argv[1], sys.argv[2]
    legs, sims, report = parse_pdf(src)
    with open(dst, 'w', encoding='utf-8') as handle:
        json.dump({'legs': legs, 'sim': sims}, handle, ensure_ascii=False)
    print(f'Legs      : {len(legs)}   '
          f'Block {report["block_min"] // 60}:'
          f'{report["block_min"] % 60:02d}')
    print(f'Landungen : {report["landings"]}')
    print(f'Sims      : {len(sims)}   '
          f'{report["sim_min"] // 60}:{report["sim_min"] % 60:02d}')
    print('KONTROLLE : OK')


if __name__ == '__main__':
    main()
