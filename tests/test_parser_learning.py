"""Structural parser-learning contracts never store source/user content."""

from pathlib import Path

from parser_learning import (
    AUDIT_INTERVAL,
    document_format_fingerprint,
    learning_mode,
    learning_read_count,
    source_evidence_hash,
)


LOG_A = """Acme Flight History
Date format YYYY-MM-DD | Block time HH:MM
2026-08-01 LH400 FRA JFK 08:15 A350 D-AIXA FO 1 0 02:15
"""

LOG_B = """Acme Flight History
Date format YYYY-MM-DD | Block time HH:MM
2026-09-17 LH123 MUC LHR 01:45 A320 D-AIXX CA 1 0 00:35
"""

ROSTER_A = """Personal Crew Schedule Report
Crew: Alice Muster 4711
Planning period: August 2026
Date Report Activity From To Start End
01 06:00 LH400 FRA JFK 08:00 16:15
"""

ROSTER_B = """Personal Crew Schedule Report
Crew: Bob Example 9822
Planning period: September 2026
Date Report Activity From To Start End
17 07:10 LH401 MUC LHR 09:00 10:45
"""


def test_same_layout_has_same_fingerprint_across_months_and_users():
    assert document_format_fingerprint(LOG_A, "logbook") == \
        document_format_fingerprint(LOG_B, "logbook")
    assert document_format_fingerprint(ROSTER_A, "roster") == \
        document_format_fingerprint(ROSTER_B, "roster")


def test_changed_column_contract_has_different_fingerprint():
    changed = LOG_B.replace(
        "Date format YYYY-MM-DD | Block time HH:MM",
        "Date | Flight | Departure | Arrival | Night | Block")
    assert document_format_fingerprint(LOG_A, "logbook") != \
        document_format_fingerprint(changed, "logbook")


def test_fingerprint_is_fixed_hash_not_source_or_pii():
    fingerprint = document_format_fingerprint(ROSTER_A, "roster")
    assert len(fingerprint) == 64
    assert all(char in "0123456789abcdef" for char in fingerprint)
    assert "Alice" not in fingerprint and "4711" not in fingerprint


def test_production_source_identity_is_keyed_not_plain_sha256():
    keyed = source_evidence_hash(ROSTER_A, "server-only-secret")
    assert keyed != source_evidence_hash(ROSTER_A)
    assert keyed == source_evidence_hash(ROSTER_A, "server-only-secret")
    assert keyed != source_evidence_hash(ROSTER_B, "server-only-secret")


def test_only_active_contract_can_reduce_to_one_read():
    assert learning_read_count(None) == 2
    assert learning_read_count({"status": "candidate", "successful_uses": 8}) == 2
    assert learning_read_count({"status": "quarantined", "successful_uses": 8}) == 2
    active = {"status": "active", "successful_uses": 2}
    assert learning_read_count(active) == 1
    assert learning_mode(active) == "active_single_read"


def test_every_tenth_success_is_a_fresh_double_read_audit():
    state = {"status": "active", "successful_uses": AUDIT_INTERVAL - 1}
    assert learning_read_count(state) == 2
    assert learning_mode(state) == "recurring_audit"


def test_learning_migration_is_service_only_and_has_no_raw_payload_columns():
    root = Path(__file__).resolve().parents[1]
    migration = next((root / "supabase" / "migrations").glob(
        "*_parser_format_learning.sql")).read_text()
    lower = migration.lower()
    assert "enable row level security" in lower
    assert "from public, anon, authenticated" in lower
    assert "security definer" in lower
    assert "to service_role" in lower
    table_section = lower.split("create table if not exists public.ax_parser_formats", 1)[1]
    table_section = table_section.split("create table if not exists", 1)[0]
    assert "source_text" not in table_section
    assert "token" not in table_section
