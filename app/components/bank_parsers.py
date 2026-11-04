"""Rule-based parsers for bank export payment advices -> ERP 'Export Proceeds Realization' record.

Supported: HSBC "Export Collection Payment Advice", SCB "Export Bill Payment Advice",
BRAC Bank "Export Bill Realization Advice".
Add a bank/layout by writing parse_<bank>() and registering it in PARSERS.

ERP structure produced:
  Bill/Invoice Amount  = amount the bank says the bill is for (claim / collection amount)
  Deductions at Source = foreign-currency charges taken out of that amount (bill amount - credited total)
  Distributions        = foreign-currency credits to the exporter's accounts
  Cumulative bill balance = Bill/Invoice Amount - (deductions + distributions)   -> must be 0
"""
import re
from datetime import datetime
from decimal import Decimal as D, ROUND_HALF_UP

from app.config.settings import settings

CENT = D("0.01")
AMT = r"([\d,]+\.\d{2})"

# ---- business rules ----------------------------------------------------------------
# Source tax = <tax %> of the BDT value of the proceeds. The % is usually 1% but some bills are taxed
# at a lower effective rate (0.83% seen), so SOURCE_TAX_RATES in settings lists the candidates.
SOURCE_TAX_RATE = D("0.01")   # default / standard rate
TAX_RATES = settings.source_tax_rates or [SOURCE_TAX_RATE]
FX_SANITY = settings.fx_sanity
FUND_RATE = D("0.0003")       # HSBC "RMG FUND-ECO" / BRAC "Central Fund" = 0.03% of the same BDT value
ACCOUNT_MAP = settings.account_map   # see app/config/account_map.json
DEDUCTION_HEAD = (ACCOUNT_MAP.get("DEDUCTION") or {}).get("account_head")


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
    # the BRAC logo is an image, so "BRAC BANK" is not in the text layer: use the email domain / SWIFT
    if "BRAC BANK" in u or "BRACBANK" in u or "BRAKBDDH" in u:
        return "BRAC"
    return None


def detect_doc_type(text: str):
    u = text.upper()
    if any(k in u for k in ("EXPORT COLLECTION PAYMENT ADVICE", "EXPORT BILL PAYMENT ADVICE",
                            "EXPORT BILL REALIZATION ADVICE", "EXPORT BILL REALISATION ADVICE")):
        return "export_payment_advice"
    if "IMPORT NEW DC OPENING ADVICE" in u:
        return "import_dc_opening"   # not for this form; route to the LC tracker
    return None


def looks_like_export_advice(text: str) -> bool:
    """Loose check used to decide whether an unknown layout is worth sending to the LLM."""
    u = text.upper()
    return "EXPORT" in u and any(k in u for k in (
        "PAYMENT ADVICE", "PROCEED", "REALISATION", "REALIZATION", "BILL PAYMENT", "COLLECTION PAYMENT"))


# ---- per-bank parsers (return a "raw" dict; build_erp_record turns it into the ERP record) -----
def parse_hsbc(t: str) -> dict:
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    value_date = re.search(r"value ([A-Za-z]+ \d{1,2}, \d{4})", t).group(1)
    charges = re.search(r"Other Bank charges\s+USD\s+" + AMT + r"\s+DR", t)
    drawee = re.search(r"Drawee:\s*(.+)", t)
    tax = re.search(r"Source TAX\s+BDT\s+" + AMT, t)
    fund = re.search(r"RMG FUND-ECO\s+BDT\s+" + AMT, t)
    return {
        "beneficiary": lines[1],  # line 0 is the letter date, line 1 the addressee
        "bill_invoice_no": re.search(r"Our Reference No\.:\s*(\S+)", t).group(1),
        "received_date": datetime.strptime(value_date, "%B %d, %Y").date(),
        "currency": re.search(r"Collection Amount:\s*([A-Z]{3})", t).group(1),
        "stated_amount": num(re.search(r"Collection Amount:\s*[A-Z]{3}\s*" + AMT, t).group(1)),
        "proceeds": num(re.search(r"Proceed Received:\s*[A-Z]{3}\s*" + AMT, t).group(1)),
        "distributions": [{"ac_suffix": m[0], "amount": num(m[2])}
                          for m in re.findall(r"credited your account number \*+(\d+) for ([A-Z]{3}) " + AMT, t)],
        "itemized_deductions": [{"label": "Other Bank charges", "amount": num(charges.group(1))}] if charges else [],
        "source_tax": num(tax.group(1)) if tax else None,
        "fund": num(fund.group(1)) if fund else None,
        "fund_tolerance": D("0.02"),
        "drawee": drawee.group(1).strip() if drawee else None,
        "lc_sc_no": None,
    }


def parse_scb(t: str) -> dict:
    d = re.search(r"DATE\s*:\s*(\d{2}[A-Za-z]{3}\d{2})", t).group(1)
    recv = re.search(r"RECEIVED PAYMENT OF\s*([A-Z]{3})\s*" + AMT, t)
    head = re.search(r"AMOUNT\s*:\s*([A-Z]{3})\s*" + AMT, t)
    mgr = re.search(r"THE MANAGER,[^\n]*\n\s*([^\n+]+)", t).group(1)
    tax = re.search(r"TAX AT SOURCE[^\n]*?BDT\s+" + AMT, t)
    # the buyer is labelled DRAWEE on collections and APPLICANT on LC-based bills
    drawee = re.search(r"DRAWEE\s*:\s*(.+)", t) or re.search(r"APPLICANT\s*:\s*(.+)", t)
    # some advices state the rate they used for the tax: that is authoritative, never derive over it
    fx = re.search(r"FX RATE USED TO CALCULATE TAX\s*:\s*(\d+(?:\.\d+)?)", t)
    return {
        "printed_rate": D(fx.group(1)) if fx else None,
        "beneficiary": re.split(r"\s{2,}", mgr.strip())[0],
        "bill_invoice_no": re.search(r"(?<![A-Z])OUR REF\s*:\s*(\S+)", t).group(1),
        "received_date": datetime.strptime(d.upper(), "%d%b%y").date(),
        "currency": recv.group(1),
        "stated_amount": num(head.group(2)),
        "proceeds": num(recv.group(2)),
        "distributions": [{"ac_no": m[0], "amount": num(m[2])}
                          for m in re.findall(r"ENTRY TO ACCOUNT\s+(\d+)\s+([A-Z]{3})\s+" + AMT + r"\s+CR", t)
                          if m[1] != "BDT"],
        "itemized_deductions": [],
        "source_tax": num(tax.group(1)) if tax else None,
        "fund": None, "fund_tolerance": D("0.02"),
        "drawee": drawee.group(1).strip() if drawee else None,
        "lc_sc_no": None,
    }


def _brac_date(s: str):
    for fmt in ("%m/%d/%Y", "%d/%m/%Y"):   # BRAC prints 10/15/2026 (month first)
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"unreadable date {s}")


def parse_brac(t: str) -> dict:
    """BRAC 'Export Bill Realization Advice'. Table cells can come out of the PDF in different line
    orders, so transaction rows are split at each account number instead of relying on columns."""
    t = re.sub(r"[ \t]+", " ", t)

    def one(pattern, flags=0):
        m = re.search(pattern, t, flags)
        return m.group(1).strip() if m else None

    drawee = one(r"Applicant/Drawee Name\s+(.*?)\s*Beneficiary/Drawer Name", re.S)
    lc = one(r"Sales Contract/LC No\s+(\S+)")
    if lc and (lc in ("Bill", "Tenor", "Exchange") or not re.fullmatch(r"[A-Za-z0-9\-/_.]+", lc)):
        lc = None
    claim = re.search(r"Claim Amount\s+([A-Z]{3})\s*" + AMT, t)
    bill_value = re.search(r"Bill Value\s+([A-Z]{3})\s*" + AMT, t)
    net = re.search(r"Net Received\s+([A-Z]{3})\s*" + AMT, t)
    stated = bill_value or claim

    # transaction rows: split the table text at every account number
    start = re.search(r"Transaction\s+particulars", t, re.I)
    end = re.search(r"We have deducted", t, re.I)
    body = t[(start.end() if start else 0):(end.start() if end else len(t))]
    accounts = list(re.finditer(r"(?<![\d\-])(\d{10,16})(?![\d\-])", body))
    credits, dates = [], []
    for i, m in enumerate(accounts):
        seg = body[m.end():(accounts[i + 1].start() if i + 1 < len(accounts) else len(body))]
        ccy = re.search(r"\b(USD|EUR|GBP|JPY|AED|BDT)\b", seg)
        amt = re.search(AMT, seg)
        side = re.search(r"\b(Cr|Dr)\b", seg)
        dt = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", seg)
        if ccy and amt and side and side.group(1) == "Cr" and ccy.group(1) != "BDT":
            credits.append({"ac_no": m.group(1), "amount": num(amt.group(1))})
            if dt:
                dates.append(dt.group(1))

    def bdt_after(label):
        m = re.search(label + r".{0,300}?" + AMT, t, re.S | re.I)
        return num(m.group(1)) if m else None

    return {
        "beneficiary": one(r"Beneficiary/Drawer Name\s+([^\n]+)"),
        "bill_invoice_no": one(r"Bill Ref No\s+(\S+)"),
        "received_date": _brac_date(dates[0] if dates else re.search(r"\d{1,2}/\d{1,2}/\d{4}", body).group(0)),
        "currency": (stated.group(1) if stated else None),
        "stated_amount": num(stated.group(2)),
        "proceeds": num(net.group(2)),
        "distributions": credits,
        "itemized_deductions": [],   # BRAC does not itemise the interest/charges in USD
        "source_tax": bdt_after(r"Advance\s+Income\s+tax"),
        "fund": bdt_after(r"Central\s+Fund"),
        "fund_tolerance": D("1.00"),   # BRAC rounds the fund to whole taka
        "drawee": re.sub(r"\s+", " ", drawee) if drawee else None,
        "lc_sc_no": lc,
    }


PARSERS = {("HSBC", "export_payment_advice"): parse_hsbc,
           ("SCB", "export_payment_advice"): parse_scb,
           ("BRAC", "export_payment_advice"): parse_brac}


# ---- conversion rate ---------------------------------------------------------------
def derive_rate(tax: D, base_fcy: D, pct: D = SOURCE_TAX_RATE):
    """Rate such that base_fcy * rate * pct reproduces the debited source tax to the cent."""
    lo = (tax - D("0.005")) / pct / base_fcy
    hi = (tax + D("0.005")) / pct / base_fcy
    rate = ((lo + hi) / 2).quantize(CENT, ROUND_HALF_UP)
    reproduces = q2(base_fcy * rate * pct) == tax
    return rate, reproduces and (hi - lo) < D("0.005")


def rate_plausible(ccy: str, rate: D) -> bool:
    band = FX_SANITY.get(ccy)
    return band is None or band[0] <= rate <= band[1]


def solve_rate(tax: D, bases, ccy: str):
    """(rate, tax %) of the first candidate that reproduces the bank's tax debit to the cent AND is a
    plausible exchange rate; (None, None) if nothing does. Lower-% candidates cannot sneak in by
    coincidence: at the wrong % the implied rate lands far outside the plausible band."""
    for pct in TAX_RATES:
        for base in bases:
            rate, ok = derive_rate(tax, base, pct)
            if ok and rate_plausible(ccy, rate):
                return rate, pct
    return None, None


def tax_pct_for_rate(tax: D, bases, rate: D):
    """Which candidate tax % reproduces the debit at a rate the bank printed (None if none does)."""
    for pct in TAX_RATES:
        for base in bases:
            if q2(base * rate * pct) == tax:
                return pct
    return None


# ---- ERP record --------------------------------------------------------------------
def _row(amount: D, rate, **extra) -> dict:
    return {**extra, "document_currency": float(amount),
            "conversion_rate": float(rate) if rate else None,
            "domestic_currency": float(q2(amount * rate)) if rate else None}


def build_erp_record(bank: str, raw: dict) -> dict:
    issues, notes = [], list(raw.get("notes", []))   # a parser (e.g. EBL xlsx) may pass its own notes
    dists = raw["distributions"]
    dist_total = sum((x["amount"] for x in dists), D("0"))
    stated = raw["stated_amount"]

    if not dists:
        issues.append("no credited-account lines found")
    if abs(dist_total - raw["proceeds"]) > D("0.005"):
        issues.append(f"credited total {dist_total} != proceeds {raw['proceeds']}")

    # Bill amount and Deductions at Source
    gap = stated - dist_total
    deductions, bill_amount = [], stated
    if gap > D("0.005"):
        items = raw.get("itemized_deductions") or []
        if items and abs(sum((i["amount"] for i in items), D("0")) - gap) <= D("0.005"):
            deductions = items
        else:
            deductions = [{"label": "Bank charges / interest (bill amount minus credited total)", "amount": gap}]
            notes.append("deduction is not itemised in the PDF: one row for the whole difference; "
                         "split it by account head if your ERP needs separate rows")
    elif gap < -D("0.005"):
        bill_amount = dist_total
        notes.append(f"bank stated amount {stated} is below the credited total {dist_total}; "
                     "bill amount set to the credited total so the balance is 0")

    # conversion rate: the bank's printed rate if there is one, else solved from the source-tax debit
    rate, tax_pct, rate_source = None, None, None
    bases = list(dict.fromkeys([dist_total, stated, raw["proceeds"]]))
    printed = raw.get("printed_rate")
    if printed:
        rate, rate_source = printed, "bank_printed"
        if raw["source_tax"] is not None:
            tax_pct = tax_pct_for_rate(raw["source_tax"], bases, rate)
            if tax_pct is None:
                notes.append(f"the bank's printed rate {rate} does not reproduce its source tax at any known tax "
                             f"rate ({', '.join(f'{p * 100:.2f}%' for p in TAX_RATES)}): rate used as printed")
    elif raw["source_tax"] is None:
        issues.append("no source-tax line: conversion rate needs manual/ERP input")
    elif dists:
        rate, tax_pct = solve_rate(raw["source_tax"], bases, raw["currency"])
        if rate is None:
            guess, _ = derive_rate(raw["source_tax"], dist_total or stated)
            issues.append(f"conversion rate could not be verified against the bank tax (1% would imply {guess}, "
                          f"outside the expected range or no match to the cent): enter it manually")
        else:
            rate_source = "derived_from_tax"
            if raw.get("fund") is not None:
                expected = (raw["source_tax"] / tax_pct) * FUND_RATE
                if abs(expected - raw["fund"]) > raw["fund_tolerance"]:
                    issues.append("derived conversion rate not verified: the fund debit does not match it")
    if tax_pct is not None and tax_pct != SOURCE_TAX_RATE:
        notes.append(f"source tax matched at {tax_pct * 100:.2f}% (not the standard {SOURCE_TAX_RATE * 100:.0f}%)")

    # distribution rows with account lookup
    dist_rows, dist_refs = [], []     # dist_refs: the account as the bank printed it (HSBC: masked)
    for x in dists:
        key = x.get("ac_no") or x.get("ac_suffix")
        dist_refs.append(x.get("ac_no") or f"******{x.get('ac_suffix')}")
        acct = ACCOUNT_MAP.get(key) or ACCOUNT_MAP.get(str(key)[-6:]) or {}
        ac_no = acct.get("ac_no") or x.get("ac_no")   # full numbers printed by the bank are used as is
        if not ac_no and x.get("ac_suffix") and settings.hsbc_account_prefix:
            ac_no = settings.hsbc_account_prefix + str(x["ac_suffix"])   # HSBC: fixed prefix + printed last 6 digits
        if not ac_no or not acct.get("account_head"):
            notes.append(f"account lookup incomplete for {key}")
        dist_rows.append(_row(x["amount"], rate, account_head=acct.get("account_head"), ac_loan_no=ac_no))

    ded_rows = [_row(d["amount"], rate, label=d["label"], account_head=DEDUCTION_HEAD) for d in deductions]
    if ded_rows and not DEDUCTION_HEAD:
        notes.append("set the DEDUCTION account head in account_map.json to pre-fill deductions")

    doc_total = dist_total + sum((d["amount"] for d in deductions), D("0"))
    balance = bill_amount - doc_total
    if abs(balance) > D("0.005"):
        issues.append(f"cumulative bill balance is {balance}")
    dom = lambda rows: sum((D(str(r["domestic_currency"])) for r in rows if r["domestic_currency"] is not None), D("0"))

    if raw.get("drawee"):
        notes.append("buyer is the bank's drawee name: check it matches the buyer in the ERP list")
    if raw.get("lc_sc_no"):
        notes.append("LC/SC No is as printed by the bank; the ERP may prefix the buyer (e.g. MnS/FTML/2025/127)")

    return {
        "bank": bank,
        "doc_type": "export_payment_advice",
        "beneficiary": raw["beneficiary"],
        "buyer": raw.get("drawee"),
        "lc_sc_no": raw.get("lc_sc_no"),
        "bill_invoice_no": raw["bill_invoice_no"],
        "received_date": raw["received_date"].isoformat(),
        "bill_invoice_date": raw["received_date"].isoformat(),   # same as received date
        "currency": raw["currency"],
        "bill_invoice_amount": float(bill_amount),
        "conversion_rate": float(rate) if rate else None,
        "deductions": ded_rows,
        "distributions": dist_rows,
        "grand_total_document": float(doc_total),
        "cumulative_bill_balance": float(balance),
        "total_domestic": float(dom(ded_rows) + dom(dist_rows)) if rate else None,
        "meta": {"bank_stated_amount": float(stated), "proceeds": float(raw["proceeds"]),
                 "distribution_refs": dist_refs,
                 "drawee": raw.get("drawee"), "rate_source": rate_source,
                 "source_tax_pct": float(tax_pct * 100) if tax_pct else None},
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
    except (AttributeError, IndexError, ValueError, KeyError):
        return None   # layout changed -> let the LLM try, and mark for review
