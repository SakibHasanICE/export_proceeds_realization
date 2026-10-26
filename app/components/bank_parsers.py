"""Rule-based parsers for bank export payment advices -> ERP 'Export Proceeds Realization' record.

Supported today: HSBC "Export Collection Payment Advice", SCB "Export Bill Payment Advice".
Add a new bank/layout by writing parse_<bank>() and registering it in PARSERS.
"""
import re
from datetime import datetime
from decimal import Decimal as D, ROUND_HALF_UP

from app.config.settings import settings

CENT = D("0.01")
AMT = r"([\d,]+\.\d{2})"

# ---- business rules (move to config/settings.py if you prefer) ----------------------
SOURCE_TAX_RATE = D("0.01")     # source tax = 1% of BDT value of the proceeds
RMG_FUND_RATE = D("0.0003")     # HSBC RMG FUND-ECO = 0.03% of the same BDT value
BILL_AMOUNT_BASIS = settings.bill_amount_basis  # "proceeds" (keeps ERP bill balance at 0) or "stated"

ACCOUNT_MAP = settings.account_map   # see app/config/account_map.json


def num(s: str) -> D:
    return D(s.replace(",", ""))


def q2(x: D) -> D:
    return x.quantize(CENT, ROUND_HALF_UP)


# ---- detection ---------------------------------------------------------------------
def detect_bank(text: str):
    u = text.upper()
    if "HONGKONG AND SHANGHAI BANKING" in u or "HSBC" in u:
        return "HSBC"
    if "STANDARD CHARTERED" in u:
        return "SCB"
    return None


def detect_doc_type(text: str):
    u = text.upper()
    if "EXPORT COLLECTION PAYMENT ADVICE" in u or "EXPORT BILL PAYMENT ADVICE" in u:
        return "export_payment_advice"
    if "IMPORT NEW DC OPENING ADVICE" in u:
        return "import_dc_opening"   # not for this form; route to the LC tracker
    return None


def looks_like_export_advice(text: str) -> bool:
    """Loose check used to decide whether an unknown layout is worth sending to the LLM."""
    u = text.upper()
    return "EXPORT" in u and any(k in u for k in (
        "PAYMENT ADVICE", "PROCEED", "REALISATION", "REALIZATION", "BILL PAYMENT", "COLLECTION PAYMENT"))


# ---- per-bank parsers --------------------------------------------------------------
def parse_hsbc(t: str) -> dict:
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    beneficiary = lines[1]  # line 0 is the letter date, line 1 the addressee
    value_date = re.search(r"value ([A-Za-z]+ \d{1,2}, \d{4})", t).group(1)
    cur = re.search(r"Collection Amount:\s*([A-Z]{3})", t).group(1)
    dists = [
        {"ac_suffix": m[0], "amount": num(m[2])}
        for m in re.findall(r"credited your account number \*+(\d+) for ([A-Z]{3}) " + AMT, t)
    ]
    charges = re.search(r"Other Bank charges\s+USD\s+" + AMT + r"\s+DR", t)
    drawee = re.search(r"Drawee:\s*(.+)", t)
    tax = re.search(r"Source TAX\s+BDT\s+" + AMT, t)
    rmg = re.search(r"RMG FUND-ECO\s+BDT\s+" + AMT, t)
    return {
        "beneficiary": beneficiary,
        "bill_invoice_no": re.search(r"Our Reference No\.:\s*(\S+)", t).group(1),
        "received_date": datetime.strptime(value_date, "%B %d, %Y").date(),
        "currency": cur,
        "stated_amount": num(re.search(r"Collection Amount:\s*[A-Z]{3}\s*" + AMT, t).group(1)),
        "proceeds": num(re.search(r"Proceed Received:\s*[A-Z]{3}\s*" + AMT, t).group(1)),
        "distributions": dists,
        "source_tax": num(tax.group(1)) if tax else None,
        "rmg_fund": num(rmg.group(1)) if rmg else None,
        "bank_charges": num(charges.group(1)) if charges else None,
        "drawee": drawee.group(1).strip() if drawee else None,
    }


def parse_scb(t: str) -> dict:
    d = re.search(r"DATE\s*:\s*(\d{2}[A-Za-z]{3}\d{2})", t).group(1)
    recv = re.search(r"RECEIVED PAYMENT OF\s*([A-Z]{3})\s*" + AMT, t)
    head = re.search(r"AMOUNT\s*:\s*([A-Z]{3})\s*" + AMT, t)
    mgr = re.search(r"THE MANAGER,[^\n]*\n\s*([^\n+]+)", t).group(1)
    beneficiary = re.split(r"\s{2,}", mgr.strip())[0]
    dists = [
        {"ac_no": m[0], "amount": num(m[2])}
        for m in re.findall(r"ENTRY TO ACCOUNT\s+(\d+)\s+([A-Z]{3})\s+" + AMT + r"\s+CR", t)
        if m[1] != "BDT"
    ]
    tax = re.search(r"TAX AT SOURCE[^\n]*?BDT\s+" + AMT, t)
    drawee = re.search(r"DRAWEE\s*:\s*(.+)", t)
    return {
        "beneficiary": beneficiary,
        "bill_invoice_no": re.search(r"(?<![A-Z])OUR REF\s*:\s*(\S+)", t).group(1),
        "received_date": datetime.strptime(d.upper(), "%d%b%y").date(),
        "currency": recv.group(1),
        "stated_amount": num(head.group(2)),
        "proceeds": num(recv.group(2)),
        "distributions": dists,
        "source_tax": num(tax.group(1)) if tax else None,
        "rmg_fund": None,
        "bank_charges": None,
        "drawee": drawee.group(1).strip() if drawee else None,
    }


PARSERS = {("HSBC", "export_payment_advice"): parse_hsbc,
           ("SCB", "export_payment_advice"): parse_scb}


# ---- conversion rate ---------------------------------------------------------------
def derive_rate(tax: D, base_fcy: D):
    """Rate such that base_fcy * rate * 1% reproduces the debited source tax to the cent."""
    lo = (tax - D("0.005")) / SOURCE_TAX_RATE / base_fcy
    hi = (tax + D("0.005")) / SOURCE_TAX_RATE / base_fcy
    rate = ((lo + hi) / 2).quantize(CENT, ROUND_HALF_UP)
    reproduces = q2(base_fcy * rate * SOURCE_TAX_RATE) == tax
    return rate, reproduces and (hi - lo) < D("0.005")


# ---- ERP record --------------------------------------------------------------------
def build_erp_record(bank: str, raw: dict) -> dict:
    issues, notes = [], []
    dists = raw["distributions"]
    dist_total = sum((x["amount"] for x in dists), D("0"))

    if not dists:
        issues.append("no credited-account lines found")
    if abs(dist_total - raw["proceeds"]) > D("0.005"):
        issues.append(f"credited total {dist_total} != proceeds {raw['proceeds']}")

    # bill amount
    bill_amount = dist_total if BILL_AMOUNT_BASIS == "proceeds" else raw["stated_amount"]
    diff = raw["stated_amount"] - dist_total
    if abs(diff) > D("0.005"):
        if raw.get("bank_charges") and abs(diff - raw["bank_charges"]) <= D("0.005"):
            notes.append(f"bank charges {raw['bank_charges']} deducted from stated amount "
                         f"{raw['stated_amount']}")
        else:
            notes.append(f"bank stated amount {raw['stated_amount']} differs from credited total {dist_total}")

    # conversion rate from source tax
    rate = None
    if raw["source_tax"] is None:
        issues.append("no source-tax line: conversion rate needs manual/ERP input")
    elif dists:
        ok_rate = False
        for base in dict.fromkeys([dist_total, raw["stated_amount"], raw["proceeds"]]):
            r, ok = derive_rate(raw["source_tax"], base)
            if rate is None or (ok and not ok_rate):
                rate, ok_rate = r, ok
        if raw.get("rmg_fund") is not None:
            expected = (raw["source_tax"] / SOURCE_TAX_RATE) * RMG_FUND_RATE
            if abs(expected - raw["rmg_fund"]) > D("0.02"):
                ok_rate = False
        if not ok_rate:
            issues.append("derived conversion rate not verified against bank tax")

    rows = []
    for x in dists:
        key = x.get("ac_no") or x.get("ac_suffix")
        acct = ACCOUNT_MAP.get(key) or ACCOUNT_MAP.get(str(key)[-6:]) or {}
        if not acct.get("ac_no") and bank == "SCB":
            acct = {**acct, "ac_no": key}
        if not acct.get("ac_no") or not acct.get("account_head"):
            notes.append(f"account lookup incomplete for {key}")
        rows.append({
            "account_head": acct.get("account_head"),
            "ac_loan_no": acct.get("ac_no"),
            "document_currency": float(x["amount"]),
            "conversion_rate": float(rate) if rate else None,
            "domestic_currency": float(q2(x["amount"] * rate)) if rate else None,
        })

    return {
        "bank": bank,
        "doc_type": "export_payment_advice",
        "beneficiary": raw["beneficiary"],
        "bill_invoice_no": raw["bill_invoice_no"],
        "received_date": raw["received_date"].isoformat(),
        "bill_invoice_date": raw["received_date"].isoformat(),  # same as received date
        "currency": raw["currency"],
        "bill_invoice_amount": float(bill_amount),
        "conversion_rate": float(rate) if rate else None,
        "distributions": rows,
        "total_domestic": float(sum((D(str(r["domestic_currency"])) for r in rows if r["domestic_currency"] is not None), D("0"))),
        "meta": {"bank_stated_amount": float(raw["stated_amount"]),
                 "proceeds": float(raw["proceeds"]),
                 "bank_charges": float(raw["bank_charges"]) if raw["bank_charges"] else None,
                 "drawee": raw["drawee"]},
        "notes": notes,
        "issues": issues,
        "needs_review": bool(issues),
    }


def parse_with_rules(text: str):
    """Returns an ERP record, or None if no rule-based parser applies/succeeds (-> LLM fallback)."""
    bank, doc_type = detect_bank(text), detect_doc_type(text)
    parser = PARSERS.get((bank, doc_type))
    if not parser:
        return {"unsupported": True, "bank": bank, "doc_type": doc_type} if doc_type else None
    try:
        return build_erp_record(bank, parser(text))
    except (AttributeError, IndexError, ValueError):
        return None   # layout changed -> let the LLM try, and mark for review
