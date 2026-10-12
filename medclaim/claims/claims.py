# claims/claims.py  --  claim submit + void
#
# Mixed vintage. submit_claim and void_claim_legacy are ORIGINAL BUILD (2018).
# void_claim (the modern path) was added with the soft-delete work in 2022
# (ADR-002). Both void paths still exist; new code should use void_claim.

import logging
import uuid
from datetime import datetime

from medclaim import db

log = logging.getLogger("medclaim.claims")


def submit_claim(patient_id, payer_id, service_date, amount_cents, adjustment_cents=0):
    claim_id = "C-" + uuid.uuid4().hex[:10]
    conn = db.connect()
    try:
        conn.execute(
            "insert into claims (id, patient_id, payer_id, service_date, "
            "amount_cents, adjustment_cents, status, is_deleted) "
            "values (?, ?, ?, ?, ?, ?, 'submitted', 0)",
            (claim_id, patient_id, payer_id, service_date,
             amount_cents, adjustment_cents),
        )
        conn.commit()
    finally:
        conn.close()
    return claim_id


def void_claim(claim_id, reason=None):
    # MODERN void path (2022, ADR-002). Flip is_deleted to 1 AND record the void.
    # This is the one new code should call.
    conn = db.connect()
    try:
        _record_void(conn, claim_id, reason, "modern")
        conn.commit()
    finally:
        conn.close()


def _record_void(conn, claim_id, reason, method):
    # T-103: the single place a void is written. Every void path must hide the
    # claim (is_deleted = 1, ADR-002) AND audit it in void_log, in the same
    # transaction. `method` keeps the audit trail of which path ran.
    conn.execute("update claims set is_deleted = 1 where id = ?", (claim_id,))
    conn.execute(
        "insert into void_log (claim_id, reason, method, voided_at) "
        "values (?, ?, ?, ?)",
        (claim_id, reason, method,
         datetime.utcnow().isoformat(timespec="seconds")),
    )


def void_claim_legacy(claim_id, reason=None):
    # ORIGINAL void path (2018). Still called from the old ERA-import and
    # correction flows. It predates the is_deleted column and used to write
    # void_log ONLY, so its voids stayed visible on statements (T-103). It now
    # goes through _record_void like the modern path.
    conn = db.connect()
    try:
        # T-103: the audit line no longer carries patient name / MRN; those must
        # never reach logs (docs/domain-primer.md). Claim id + patient id are
        # enough to trace it.
        row = conn.execute(
            "select patient_id from claims where id = ?", (claim_id,)
        ).fetchone()
        if row is not None:
            log.info("legacy void: claim=%s pat=%s reason=%s",
                     claim_id, row["patient_id"], reason)
        _record_void(conn, claim_id, reason, "legacy")
        conn.commit()
    finally:
        conn.close()


def backfill_legacy_voids():
    # T-103 data repair. Claims voided through the legacy path BEFORE the fix
    # have a void_log row but is_deleted still NULL/0, so they keep showing on
    # statements. void_log is the record of truth for voids (ADR-002); flag
    # every claim it lists. Idempotent: safe to re-run. Returns rows fixed.
    conn = db.connect()
    try:
        cur = conn.execute(
            "update claims set is_deleted = 1 "
            "where is_deleted is not 1 "
            "and id in (select claim_id from void_log)"
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
