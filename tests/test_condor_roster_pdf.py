"""Condor calendar adapters: UTC CUBE duty plan and strict controls."""

from condor_roster_pdf import duty_plan_events


UTC_PLAN = """CP 442448H Example
Duty plan requested at 11AUG26 12:24z - All times: UTC - Page 1 of 1
BT DH EQH BZW Off claim Off assigned
03:00 00:00 03:00 03:00 1 1 07/2026
Date Duty Type Reg Pos Crew P/U C/I Routing Diff Hotel BT
P We 1 ORT MUC
P Th 2 SB90 MUC 04:00 - 16:00
P Fr 3 DE1000 32N DANCA CP C1 08:00 MUC 09:00 - 10:30 PMI 01:30
P DE1001 32N DANCA CP C1 PMI 11:30 - 13:00 MUC 01:30
P DH/LH2000 32N DANCA MUC 14:00 - 15:00 FRA 01:00
P Sa 4 OFF MUC
"""


def test_utc_duty_plan_keeps_operating_legs_and_ground_days():
    events, year, month, report, error = duty_plan_events(UTC_PLAN)
    assert error is None and (year, month) == (2026, 7)
    assert report['operating_block_min'] == 180
    summaries = [event[3] for event in events]
    assert summaries == [
        'Off Day', 'SB90 MUC', 'DE1000 MUC - PMI',
        'DE1001 PMI - MUC', 'DH/LH2000 MUC - FRA', 'Off Day',
    ]
    assert events[2][1].isoformat() == '2026-07-03T09:00:00'


def test_duty_plan_rejects_a_block_total_mismatch():
    _, _, _, _, error = duty_plan_events(UTC_PLAN.replace('03:00 00:00',
                                                           '03:01 00:00'))
    assert error == 'condor_block_total_mismatch'


def test_duty_plan_refuses_unknown_time_basis():
    result = duty_plan_events(UTC_PLAN.replace('All times: UTC',
                                               'All times: Mars'))
    assert result[-1] == 'unsupported_pdf_format'
