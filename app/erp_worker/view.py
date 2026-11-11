"""Turns a database entry into (a) what the ERP form will receive and (b) what the web table shows.

No browser and no web framework in here, so it can be tested on its own.
"""
from app.config.settings import _load_account_map


def _head_index() -> dict[str, str]:
    idx: dict[str, str] = {}
    for key, v in _load_account_map().items():
        head = ((v or {}).get("account_head") or "").strip()
        if key.startswith("DEDUCTION") or not head:
            continue
        idx[str(key)] = head
        if (v or {}).get("ac_no"):
            idx[str(v["ac_no"])] = head
    return idx


def head_for_account(ac_no, db_head=None):
    """(account head, where it came from) - the database value wins, then account_map.json."""
    if db_head and str(db_head).strip():
        return str(db_head).strip(), "database"
    if not ac_no:
        return None, None
    idx = _head_index()
    a = str(ac_no).strip()
    if a in idx:
        return idx[a], "account map"
    if len(a) >= 6 and a[-6:] in idx:
        return idx[a[-6:]], "account map"
    return None, None


def deduction_head(db_head=None, label=None):
    if db_head and str(db_head).strip():
        return str(db_head).strip(), "database"
    m = _load_account_map()
    for key in (f"DEDUCTION:{(label or '').strip().lower()}", "DEDUCTION"):
        head = ((m.get(key) or {}).get("account_head") or "").strip()
        if head:
            return head, "account map"
    return None, None


def build_run_payload(rec: dict) -> dict:
    """What the robot types into the ERP form (heads resolved from account_map.json when the DB has none)."""
    from app.components import database   # lazy: keeps this module importable without a database
    payload = database.erp_payload(rec)
    for row, src in zip(payload["Deductions at Source"], rec["deductions"]):
        row["Account Head"] = deduction_head(src.get("account_head"), src.get("label"))[0]
    for row, src in zip(payload["Distributions"], rec["distributions"]):
        row["Account Head"] = head_for_account(src.get("ac_loan_no"), src.get("account_head"))[0]
    return payload


def build_erp_view(rec: dict) -> dict:
    """One document as the web table shows it: every value the robot will fill, plus blockers/warnings."""
    rate = rec.get("conversion_rate")

    def line(x: dict, dist: bool) -> dict:
        if dist:
            head, src = head_for_account(x.get("ac_loan_no"), x.get("account_head"))
        else:
            head, src = deduction_head(x.get("account_head"), x.get("label"))
        out = {"account_head": head, "account_head_source": src,
               "document_currency": x.get("document_currency"),
               "conversion_rate": x.get("conversion_rate") if x.get("conversion_rate") is not None else rate,
               "domestic_currency": x.get("domestic_currency")}
        if dist:
            out["ac_loan_no"] = x.get("ac_loan_no")
        else:
            out["label"] = x.get("label")
        return out

    deds = [line(x, False) for x in rec.get("deductions", [])]
    dists = [line(x, True) for x in rec.get("distributions", [])]
    s = lambda rows, k: round(sum(float(r[k] or 0) for r in rows), 2)

    blockers: list[str] = []
    if rec.get("status") == "filled":
        blockers.append("already marked as filled")
    for f, label in (("beneficiary", "Beneficiary"), ("bill_invoice_no", "Bill/Invoice No"),
                     ("received_date", "Received Date")):
        if not rec.get(f):
            blockers.append(f"{label} is empty")
    if not dists:
        blockers.append("no distribution rows")
    for i, d in enumerate(dists, 1):
        if not d["ac_loan_no"]:
            blockers.append(f"distribution {i}: AC / Loan No is empty")
        if not d["document_currency"]:
            blockers.append(f"distribution {i}: amount is empty")

    warnings: list[str] = []
    bal = rec.get("cumulative_bill_balance")
    if dists and bal is not None and abs(bal) > 0.005:
        warnings.append(f"cumulative bill balance in the database is {bal:.2f} (should be 0)")
    no_head = sum(1 for d in dists if not d["account_head"]) + sum(1 for d in deds if not d["account_head"])
    if no_head:
        warnings.append(f"{no_head} row(s) without an Account Head (they stay empty in the ERP)")
    if rec.get("needs_review"):
        warnings.append("marked 'needs review' by the extractor")
    for issue in rec.get("issues") or []:
        if str(issue) not in " ".join(warnings):
            warnings.append(str(issue))

    # one very short remark for the table: what blocks this entry, or what is uncertain about it
    errs: list[str] = []
    for f, label in (("beneficiary", "Beneficiary"), ("bill_invoice_no", "Bill No"), ("received_date", "Received Date")):
        if not rec.get(f):
            errs.append(f"No {label}")
    if not dists:
        errs.append("No distribution rows")
    for i, d in enumerate(dists, 1):
        if not d["ac_loan_no"]:
            errs.append(f"Dist {i}: no AC/Loan No")
        elif not d["document_currency"]:
            errs.append(f"Dist {i}: no amount")
    warns: list[str] = []
    if dists and bal is not None and abs(bal) > 0.005:
        warns.append(f"Balance {bal:,.2f}, should be 0")
    if rec.get("needs_review"):
        first = (rec.get("issues") or [None])[0]
        warns.append(f"Check: {first}" if first else "Needs review")
    if rec.get("status") == "filled":
        remark = {"level": "info", "text": "Saved in the ERP"}
    elif errs:
        remark = {"level": "error", "text": "; ".join(errs[:2])[:90]}
    elif warns:
        remark = {"level": "warn", "text": "; ".join(warns[:2])[:90]}
    else:
        remark = None

    return {
        "remark": remark,
        "id": rec["id"], "bank": rec.get("bank"), "bill_invoice_no": rec.get("bill_invoice_no"),
        "status": rec.get("status"), "needs_review": rec.get("needs_review"),
        "source_file": rec.get("source_file"), "created_at": rec.get("created_at"),
        "beneficiary": rec.get("beneficiary"), "buyer": rec.get("buyer"), "lc_sc_no": rec.get("lc_sc_no"),
        "received_date": rec.get("received_date"), "bill_invoice_date": rec.get("bill_invoice_date"),
        "currency": rec.get("currency"), "bill_invoice_amount": rec.get("bill_invoice_amount"),
        "conversion_rate": rate,
        "deductions": deds, "distributions": dists,
        "totals": {"deductions_document": s(deds, "document_currency"), "deductions_domestic": s(deds, "domestic_currency"),
                   "distributions_document": s(dists, "document_currency"),
                   "distributions_domestic": s(dists, "domestic_currency"),
                   "grand_document": rec.get("grand_total_document"), "grand_domestic": rec.get("grand_total_domestic"),
                   "cumulative_bill_balance": bal},
        "erp_fills_itself": ["Buyer", "LC/SC No", "Currency", "Bill/Invoice Date", "Bill/Invoice Amount"],
        "can_run": not blockers, "blockers": blockers, "warnings": warnings,
    }
