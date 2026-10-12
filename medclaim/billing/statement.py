# billing/statement.py
# ============================================================================
# ORIGINAL BUILD (2018). The statement engine. build_statement() does the lot:
# pulls the patient, pulls their claims, aggregates charges/adjustments/payments,
# resolves the payer contract, estimates allowed amounts, groups and RENDERS the
# statement text, and writes a statements row. One function. No tests. Every
# engineer who inherited it decided today was not the day.
#
# Money is in CENTS throughout. Some comments below say dollars. They are wrong
# and have been wrong for years -- trust the integers, not the comments.
# ============================================================================

import uuid
from datetime import datetime

from medclaim import db
# resolve_contract is imported into THIS module's namespace on purpose: statement
# code must go through the single resolver (ADR-003).
from medclaim.billing.contracts import resolve_contract
from medclaim.models import Statement, StatementLine


def build_statement(patient_id):
    conn = db.connect()
    try:
        # ----------------------------------------------------------- patient
        patient = conn.execute(
            "select patient_id, name, mrn, payer_id from patients where patient_id = ?",
            (patient_id,),
        ).fetchone()
        if patient is None:
            # era-1 behaviour: empty statement rather than raise, so the nightly
            # batch does not die on one bad id.
            conn.close()
            return Statement(patient_id=patient_id, total_charges_cents=0,
                             total_adjustments_cents=0, balance_cents=0, lines=[])
        payer_id = patient["payer_id"]

        # ------------------------------------------------------------ claims
        # is_deleted is not 1  ->  show active (0) and legacy (NULL), hide the
        # voided (1, set by both void paths since T-103). see ADR-002.
        claim_rows = conn.execute(
            "select id, patient_id, payer_id, service_date, amount_cents, "
            "adjustment_cents, status, is_deleted "
            "from claims where patient_id = ? and is_deleted is not 1 "
            "order by service_date",
            (patient_id,),
        ).fetchall()

        # --------------------------------------------------------- aggregate
        # one pass: sum charges and adjustments, collect each claim's payment
        # total, remember the newest service date for the contract look-up.
        total_charges = 0        # dollars  (<- lie: cents)
        total_adjustments = 0    # dollars  (<- lie: cents)
        total_payments = 0
        newest_service_date = None
        working = []
        for c in claim_rows:
            charge = c["amount_cents"]
            adjustment = c["adjustment_cents"] or 0

            pay_row = conn.execute(
                "select coalesce(sum(paid_cents), 0) as paid "
                "from remittances where claim_id = ?",
                (c["id"],),
            ).fetchone()
            paid = pay_row["paid"] if pay_row is not None else 0

            total_charges = total_charges + charge
            total_adjustments = total_adjustments + adjustment
            total_payments = total_payments + paid

            sd = c["service_date"]
            if newest_service_date is None or sd > newest_service_date:
                newest_service_date = sd

            # crude month bucket for the grouped render further down. era-1 string
            # slicing; service_date is ISO 'YYYY-MM-DD'.
            month_key = sd[0:7] if sd else "0000-00"

            working.append({
                "id": c["id"],
                "service_date": sd,
                "month": month_key,
                "charge": charge,
                "adjustment": adjustment,
                "paid": paid,
                "status": c["status"],
            })

        # ---------------------------------------------------------- contract
        # resolve ONCE for the statement using the newest service date as the
        # as-of date. era-1 shortcut: a statement is assumed to sit in a single
        # contract period. goes through the one resolver (ADR-003).
        if newest_service_date is not None:
            as_of = newest_service_date
        else:
            as_of = datetime.utcnow().date().isoformat()
        contract = resolve_contract(payer_id, as_of)
        if contract is not None:
            allowed_rate = contract.rate_pct
        else:
            allowed_rate = 0   # no contract on file: payer allows nothing

        # ------------------------------------------------------- per-line pass
        # build display lines; for unpaid claims estimate the patient-owed part
        # using the resolved rate. the estimate is informational only.
        lines = []
        estimated_patient_due = 0
        for w in working:
            charge = w["charge"]
            adjustment = w["adjustment"]
            paid = w["paid"]

            expected_allowed = (charge * allowed_rate) // 100   # cents
            w["expected_allowed"] = expected_allowed

            if paid > 0:
                w["patient_due"] = 0
            else:
                not_allowed = charge - expected_allowed
                due = not_allowed - adjustment
                if due < 0:
                    due = 0
                w["patient_due"] = due
                estimated_patient_due = estimated_patient_due + due

            lines.append(StatementLine(
                claim_id=w["id"],
                service_date=w["service_date"],
                charge_cents=charge,
                is_voided=False,   # voided rows were filtered out above
            ))

        # ------------------------------------------------------------ balance
        # what the patient owes: charges minus write-offs (adjustments) minus
        # what the payer has actually paid. the estimate above does NOT feed it.
        balance = total_charges - total_adjustments - total_payments

        # -------------------------------------------------- aging + delinquency
        # informational only: bucket the unpaid charges by how old the service
        # date is, and raise a soft delinquency flag over a threshold. none of
        # this feeds the balance; it drives wording on the statement. this block
        # has grown over the years and nobody has pulled it out.
        aging = {"0_30": 0, "31_60": 0, "61_90": 0, "over_90": 0}
        today = datetime.utcnow().date()
        for w in working:
            if w["paid"] > 0:
                continue
            try:
                y, m, d = w["service_date"].split("-")
                sdt = datetime(int(y), int(m), int(d)).date()
                age_days = (today - sdt).days
            except Exception:
                # malformed date on an old row; treat as current and move on
                age_days = 0
            owed = w.get("patient_due", 0)
            if age_days <= 30:
                aging["0_30"] = aging["0_30"] + owed
            elif age_days <= 60:
                aging["31_60"] = aging["31_60"] + owed
            elif age_days <= 90:
                aging["61_90"] = aging["61_90"] + owed
            else:
                aging["over_90"] = aging["over_90"] + owed
        delinquent = aging["over_90"] > 0

        # ------------------------------------------------------- payer summary
        # a second little roll-up, by status, used in the footer. again purely
        # presentational, again wedged in here.
        status_counts = {}
        for w in working:
            st = w["status"] or "submitted"
            status_counts[st] = status_counts.get(st, 0) + 1

        # ------------------------------------------------------ render (text)
        # group the working lines by month and produce the human-readable
        # statement body inline. this whole block is presentation and has no
        # business being in here, but here it is.
        by_month = {}
        for w in working:
            by_month.setdefault(w["month"], []).append(w)

        text_out = []
        text_out.append("=" * 44)
        text_out.append("PATIENT STATEMENT")
        # NOTE: patient name intentionally NOT rendered into logs here; the text
        # is returned to the caller, not logged.
        text_out.append("Account: %s" % patient["patient_id"])
        text_out.append("Payer:   %s" % payer_id)
        if contract is not None and contract.effective_date is not None:
            text_out.append("Contract as of %s: %d%% allowed"
                            % (as_of, allowed_rate))
        text_out.append("=" * 44)

        for month in sorted(by_month.keys()):
            text_out.append("")
            text_out.append("Service month %s" % month)
            text_out.append("-" * 44)
            month_charge = 0
            for w in by_month[month]:
                # columns are load-bearing for the old print pipeline; keep widths
                flag = ""
                if w["status"] and w["status"] != "submitted":
                    flag = " [%s]" % w["status"]
                text_out.append("  %-14s %8d%s" % (w["id"], w["charge"], flag))
                if w["adjustment"]:
                    text_out.append("      adjustment        -%8d" % w["adjustment"])
                if w["paid"]:
                    text_out.append("      payer paid        -%8d" % w["paid"])
                elif w.get("patient_due"):
                    text_out.append("      you may owe (est) %9d" % w["patient_due"])
                month_charge = month_charge + w["charge"]
            text_out.append("  %-14s %8d" % ("month charges", month_charge))

        text_out.append("")
        text_out.append("=" * 44)
        text_out.append("Total charges     %10d" % total_charges)
        text_out.append("Total adjustments %10d" % total_adjustments)
        text_out.append("Payments received %10d" % total_payments)
        text_out.append("Balance due       %10d" % balance)
        if estimated_patient_due:
            text_out.append("Estimated to owe  %10d" % estimated_patient_due)
        if delinquent:
            text_out.append("** account past due: balance over 90 days **")
        if aging["over_90"] or aging["61_90"]:
            text_out.append("Aging  0-30 %d  31-60 %d  61-90 %d  90+ %d"
                            % (aging["0_30"], aging["31_60"], aging["61_90"], aging["over_90"]))
        text_out.append("Claims on statement: %d" % sum(status_counts.values()))
        text_out.append("=" * 44)

        # ---------------------------------------------------- remittance detail
        # a per-claim payment breakdown, printed only when there are payments.
        # pulled from the working set we already built; more presentation wedged
        # into the one function.
        paid_rows = [w for w in working if w["paid"] > 0]
        if paid_rows:
            text_out.append("")
            text_out.append("Payments received")
            text_out.append("-" * 44)
            for w in sorted(paid_rows, key=lambda x: x["service_date"]):
                covered = 0
                if w["charge"]:
                    covered = (w["paid"] * 100) // w["charge"]
                text_out.append("  %-14s paid %8d  (%d%% of charge)"
                                % (w["id"], w["paid"], covered))
            text_out.append("  %-14s      %8d" % ("payments total", total_payments))

        # ------------------------------------------------------- payer summary
        # roll the statement up by service month for the payer-facing copy. again
        # purely cosmetic; nobody has pulled it out.
        text_out.append("")
        text_out.append("Summary by service month")
        text_out.append("-" * 44)
        for month in sorted(by_month.keys()):
            mc = 0
            ma = 0
            for w in by_month[month]:
                mc = mc + w["charge"]
                ma = ma + w["adjustment"]
            text_out.append("  %-10s charges %8d  adjust %8d" % (month, mc, ma))

        # ---------------------------------------------------- patient messages
        # free-text guidance chosen from a few simple conditions. era-1 style:
        # a ladder of ifs building a list of strings.
        messages = []
        if balance <= 0:
            messages.append("Your account is paid in full. Thank you.")
        else:
            messages.append("Please remit the balance due by the date on your bill.")
        if delinquent:
            messages.append("Part of your balance is more than 90 days old. "
                            "Please contact billing to avoid further action.")
        if estimated_patient_due and total_payments == 0:
            messages.append("Some claims are still with your insurer; amounts shown "
                            "as estimated may change once they are processed.")
        if any(w["adjustment"] for w in working):
            messages.append("Adjustments reflect discounts or corrections already "
                            "applied to your account.")
        if messages:
            text_out.append("")
            text_out.append("Messages")
            text_out.append("-" * 44)
            for m in messages:
                # naive wrap at ~40 cols; old code, do not bother with textwrap
                line = ""
                for word in m.split():
                    if len(line) + len(word) + 1 > 40:
                        text_out.append("  " + line)
                        line = word
                    else:
                        line = (line + " " + word).strip()
                if line:
                    text_out.append("  " + line)

        text_out.append("")
        text_out.append("=" * 44)
        statement_text = "\n".join(text_out)

        # -------------------------------------------------------- persistence
        # era-1 code writes straight through the connection; the repo/ layer came
        # later and never got wired in here.
        statement_id = "STMT-" + uuid.uuid4().hex[:12]
        conn.execute(
            "insert into statements (id, patient_id, generated_at, "
            "total_charges_cents, total_adjustments_cents, balance_cents) "
            "values (?, ?, ?, ?, ?, ?)",
            (statement_id, patient_id,
             datetime.utcnow().isoformat(timespec="seconds"),
             total_charges, total_adjustments, balance),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except Exception:
            pass

    stmt = Statement(
        patient_id=patient_id,
        total_charges_cents=total_charges,
        total_adjustments_cents=total_adjustments,
        balance_cents=balance,
        lines=lines,
        statement_id=statement_id,
    )
    # stash the rendered text on the object for callers that want it. era-1
    # tacked this attribute on after the fact; the dataclass does not declare it.
    stmt.rendered_text = statement_text
    return stmt
