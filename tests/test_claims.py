import logging

from medclaim import db
from medclaim.billing.history import get_statement_history
from medclaim.billing.statement import build_statement
from medclaim.claims import (
    backfill_legacy_voids,
    submit_claim,
    void_claim,
    void_claim_legacy,
)


def test_submit_creates_active_claim(client, seed):
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-01", 12_000, 0)
    ids = [l.claim_id for l in get_statement_history("P-1042")]
    assert cid in ids


def test_modern_void_hides_claim_from_history(client):
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-02", 5_000, 0)
    void_claim(cid, reason="entered in error")
    ids = [l.claim_id for l in get_statement_history("P-1042")]
    assert cid not in ids


def test_modern_void_records_in_void_log(client):
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-03", 5_000, 0)
    void_claim(cid, reason="dupe")
    conn = db.connect()
    try:
        row = conn.execute(
            "select method from void_log where claim_id = ?", (cid,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["method"] == "modern"


def test_legacy_void_records_in_void_log(client):
    # the legacy path still records the void in the audit log.
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-04", 5_000, 0)
    void_claim_legacy(cid, reason="correction")
    conn = db.connect()
    try:
        row = conn.execute(
            "select method from void_log where claim_id = ?", (cid,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row["method"] == "legacy"


def test_legacy_void_hides_claim_from_history(client):
    # T-103 reproduction: a claim voided through the legacy path has a void_log
    # row but stayed on statement history because is_deleted was never set.
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-05", 5_000, 0)
    void_claim_legacy(cid, reason="correction")
    ids = [l.claim_id for l in get_statement_history("P-1042")]
    assert cid not in ids


def test_legacy_void_hides_claim_from_statement(client):
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-06", 5_000, 0)
    void_claim_legacy(cid, reason="correction")
    stmt = build_statement("P-1042")
    assert cid not in [l.claim_id for l in stmt.lines]


def test_legacy_void_keeps_legacy_method_in_void_log(client):
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-07", 5_000, 0)
    void_claim_legacy(cid, reason="correction")
    conn = db.connect()
    try:
        rows = conn.execute(
            "select method from void_log where claim_id = ?", (cid,)
        ).fetchall()
    finally:
        conn.close()
    assert [r["method"] for r in rows] == ["legacy"]


def test_legacy_void_does_not_log_patient_name_or_mrn(client, caplog):
    conn = db.connect()
    try:
        patient = conn.execute(
            "select name, mrn from patients where patient_id = 'P-1042'"
        ).fetchone()
    finally:
        conn.close()
    cid = submit_claim("P-1042", "BCBS-001", "2024-07-08", 5_000, 0)
    with caplog.at_level(logging.INFO, logger="medclaim.claims"):
        void_claim_legacy(cid, reason="correction")
    assert cid in caplog.text
    assert patient["name"] not in caplog.text
    assert patient["mrn"] not in caplog.text


def _legacy_void_before_fix(claim_id):
    # simulates a void written by the pre-T-103 legacy path: audit row only,
    # is_deleted left untouched.
    conn = db.connect()
    try:
        conn.execute(
            "insert into void_log (claim_id, reason, method, voided_at) "
            "values (?, 'old correction', 'legacy', '2023-01-01T00:00:00')",
            (claim_id,),
        )
        conn.commit()
    finally:
        conn.close()


def test_backfill_hides_claims_voided_by_old_legacy_path(client):
    stale = submit_claim("P-1042", "BCBS-001", "2024-07-09", 5_000, 0)
    active = submit_claim("P-1042", "BCBS-001", "2024-07-10", 5_000, 0)
    _legacy_void_before_fix(stale)
    assert stale in [l.claim_id for l in get_statement_history("P-1042")]

    assert backfill_legacy_voids() == 1

    ids = [l.claim_id for l in get_statement_history("P-1042")]
    assert stale not in ids
    assert active in ids


def test_backfill_keeps_era1_null_claims_visible_and_is_idempotent(client):
    # seed claims are era-1 rows (is_deleted NULL) and must stay visible.
    before = [l.claim_id for l in get_statement_history("P-1042")]
    assert before
    assert backfill_legacy_voids() == 0
    assert backfill_legacy_voids() == 0
    assert [l.claim_id for l in get_statement_history("P-1042")] == before
