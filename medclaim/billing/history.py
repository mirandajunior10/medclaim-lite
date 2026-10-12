# billing/history.py  --  statement history
#
# ORIGINAL BUILD (2018), patched in 2022 for the soft-delete work (ADR-002).
# The is_deleted IS NOT 1 filter is deliberate: it shows active (0) AND the old
# era-1 rows (NULL), and hides voided rows (1). Both void paths set 1 (T-103).

from medclaim import db
from medclaim.models import StatementLine


def get_statement_history(patient_id):
    conn = db.connect()
    try:
        rows = conn.execute(
            "select id, service_date, amount_cents, is_deleted "
            "from claims where patient_id = ? "
            "and is_deleted is not 1 "
            "order by service_date",
            (patient_id,),
        ).fetchall()
    finally:
        conn.close()
    lines = []
    for r in rows:
        lines.append(StatementLine(
            claim_id=r["id"],
            service_date=r["service_date"],
            charge_cents=r["amount_cents"],
            is_voided=(r["is_deleted"] == 1),
        ))
    return lines
