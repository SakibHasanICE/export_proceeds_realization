"""EBL "EXPORT BILLS REALIZATION SHEET" (.xlsx): one worksheet holds many bills -> one ERP record per bill.

Unlike the bank PDFs this is a spreadsheet prepared at the bank, so nothing is derived: the conversion rate,
the amounts and the account numbers are all stated in the cells. Columns are found by their header text, not
by letter, so inserting a column does not break it.

Mapping used for the ERP form (change DISTRIBUTIONS below if your ERP entry differs):
  Bill/Invoice Amount   = BILL AMT. (USD)
  Deductions at source  = SHORT AMT. (bill amount - proceeds), USD
  Distributions         = MARGIN, CM AMT., ERQ, MARGIN ON OTHER IMPORT OBLIGATIONS, each to the account
                          printed in the same row; zero amounts are skipped
  Conversion rate       = EXCHANGE RATE of the row
BDT charges on the sheet (commission, courier, export charge, govt tax, central fund) are NOT part of this
form, the same as the BDT debits on the HSBC/SCB/BRAC advices; they are kept in `meta` for reference.
"""
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal as D, ROUND_HALF_UP
from pathlib import Path

import openpyxl

from app.components.bank_parsers import build_erp_record

BANK = "EBL"
CENT = D("0.01")

# (amount column, account column, label for the note). Header text, upper-cased, single-spaced.
DISTRIBUTIONS = [
    ("MARGIN", "MARGIN CORP.", "margin"),
    ("CM AMT.", "UGL CD", "CM amount"),
    ("ERQ", "UGL ERQ", "ERQ"),
    ("MARGIN ON OTHER IMPORT OBLIGATIONS", "MARGIN ON OTHER IMPORT OBLIGATIONS (15 DAYS)",
     "margin on other import obligations"),
]
BDT_CHARGES = {"commission": "COMMISSION ON EXPORT BILL", "courier": "COUR. CHG.", "export_charge": "EXP CHG.",
               "govt_tax": "GOVT. TAX", "central_fund": "CENTRAL FUND"}

BILL_NO = re.compile(r"^(?=.*\d{4})[A-Z0-9]{8,}$")   # FDBC1040275926; rejects labels like "TODAY'S TT (DOC)"


def _norm(v) -> str:
    return " ".join(str(v).upper().split()) if v is not None else ""


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return D(str(v))
    try:
        return D(str(v).replace(",", "").strip())
    except Exception:
        return None


def _q(x: D) -> D:
    return x.quantize(CENT, ROUND_HALF_UP)


def _acct(v):
    """Account numbers are stored as text or as numbers; keep them as plain digit strings."""
    if v is None or str(v).strip() == "":
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def is_workbook(path: Path | str) -> bool:
    """True if the file really is an .xlsx (a zip with xl/workbook.xml); the extension is not trusted."""
    try:
        with zipfile.ZipFile(path) as z:
            return "xl/workbook.xml" in z.namelist()
    except (zipfile.BadZipFile, OSError):
        return False


def _find_header(ws):
    for r in range(1, min(ws.max_row, 60) + 1):
        cols = {}
        for c in range(1, ws.max_column + 1):
            h = _norm(ws.cell(r, c).value)
            if h and h not in cols:
                cols[h] = c
        if "BILL NO." in cols and any(h.startswith("PROCEEDS") for h in cols):
            return r, cols
    return None, None


def _correspondent_value_dates(ws) -> dict:
    """bill no -> value date, from the 'CORRESPONDENT BANK NAME' table (only some bills are listed)."""
    out = {}
    for r in range(1, ws.max_row + 1):
        if _norm(ws.cell(r, 1).value) != "CORRESPONDENT BANK NAME":
            continue
        cols = {_norm(ws.cell(r, c).value): c for c in range(1, ws.max_column + 1) if ws.cell(r, c).value}
        vd, bn = cols.get("VALUE DATE"), cols.get("BILL NO.")
        if not (vd and bn):
            break
        for rr in range(r + 1, ws.max_row + 1):
            bill, val = ws.cell(rr, bn).value, ws.cell(rr, vd).value
            if bill and isinstance(val, (date, datetime)):
                out[str(bill).strip()] = (val.date() if isinstance(val, datetime) else val).isoformat()
        break
    return out


def _parse_sheet(ws) -> list[dict]:
    hdr, cols = _find_header(ws)
    if hdr is None:
        return []

    def cell(r, name):
        c = cols.get(name)
        return ws.cell(r, c).value if c else None

    def col_by_prefix(prefix):
        return next((c for h, c in cols.items() if h.startswith(prefix)), None)

    # beneficiary + sheet date sit above the table
    beneficiary, sheet_date = None, None
    for r in range(1, hdr):
        v = ws.cell(r, 1).value
        if isinstance(v, (date, datetime)):
            sheet_date = v.date() if isinstance(v, datetime) else v
        elif isinstance(v, str) and v.strip() and "REALIZATION" not in v.upper() and not beneficiary:
            beneficiary = v.strip()
    if not sheet_date or not beneficiary:
        raise ValueError("beneficiary name or sheet date not found above the table")

    ccy = (re.search(r"\((\w{3})\)", str(ws.cell(hdr, cols["BILL AMT. (USD)"]).value))
           if "BILL AMT. (USD)" in cols else None)
    currency = ccy.group(1) if ccy else "USD"

    # fallback rates printed under the table
    sheet_rates = {}
    for r in range(hdr + 1, ws.max_row + 1):
        label = _norm(ws.cell(r, 1).value)
        if label.startswith("TODAY'S SPECIAL RATE") or label.startswith("TODAY'S TT"):
            n = _num(ws.cell(r, 2).value)
            if n:
                sheet_rates[label] = n
    fallback_rate = sheet_rates.get("TODAY'S SPECIAL RATE") or sheet_rates.get("TODAY'S TT (DOC)")

    value_dates = _correspondent_value_dates(ws)
    amount_col = col_by_prefix("BILL AMT")
    proceeds_col = col_by_prefix("PROCEEDS")

    records, last_row = [], hdr
    for r in range(hdr + 1, ws.max_row + 1):
        if _norm(ws.cell(r, 1).value).startswith("TODAY'S"):
            break                                    # everything below is the rates / accounts block
        bill = _norm(ws.cell(r, 1).value).replace(" ", "")
        b, c = _num(ws.cell(r, amount_col).value), _num(ws.cell(r, proceeds_col).value)
        if not BILL_NO.match(bill) or b is None or c is None or b <= 0:
            continue
        last_row = r
        notes, meta = [], {"sheet": ws.title, "sheet_row": r}

        rate = _num(cell(r, "EXCHANGE RATE"))
        if not rate:
            rate = fallback_rate
            notes.append("no exchange rate on this row: the sheet's 'today's rate' was used")

        # amounts: the sheet's own cells; computed only if the workbook was never calculated
        e = _num(cell(r, "CM AMT.")) or D("0")
        margin = _num(cell(r, "MARGIN"))
        if margin is None:
            margin = (b * D("0.8")).quantize(D("1"), ROUND_HALF_UP)
        erq = _num(cell(r, "ERQ"))
        if erq is None:
            erq = (c * D("0.075")).quantize(D("1"), ROUND_HALF_UP)
        other = _num(cell(r, "MARGIN ON OTHER IMPORT OBLIGATIONS"))
        if other is None:
            other = c - margin - e - erq
        amounts = {"MARGIN": margin, "CM AMT.": e, "ERQ": erq, "MARGIN ON OTHER IMPORT OBLIGATIONS": other}

        dists, parts = [], []
        for amount_col_name, acct_col_name, label in DISTRIBUTIONS:
            amt = _q(amounts[amount_col_name])
            if amt == 0:
                continue
            acct = _acct(cell(r, acct_col_name))
            dists.append({"amount": amt, "ac_no": acct})
            parts.append(f"{label} {amt:,.2f} -> {acct or 'no account on the sheet'}")
        notes.append("distribution rows, in order: " + "; ".join(parts))

        short = _q(b - c)
        itemized = [{"label": "Short amount (bill amount minus proceeds)", "amount": short}] if short > D("0.005") else []

        vd = value_dates.get(bill)
        if vd:
            meta["correspondent_value_date"] = vd
            notes.append(f"received date is the sheet date {sheet_date}; correspondent value date is {vd}")
        meta["bdt_charges"] = {k: float(_num(cell(r, h)) or 0) for k, h in BDT_CHARGES.items()}
        notes.append("BDT charges on the sheet (commission, courier, export charge, govt tax, central fund) "
                     "are not part of this form; they are stored in the entry's meta")
        notes.append("buyer is not stated in the EBL sheet: fill it in manually")

        lc = cell(r, "LC/CONTRACT NO.")
        raw = {"beneficiary": beneficiary, "drawee": None, "lc_sc_no": str(lc).strip() if lc else None,
               "bill_invoice_no": bill, "received_date": sheet_date, "currency": currency,
               "stated_amount": b, "proceeds": c, "source_tax": None, "fund": None,
               "printed_rate": rate, "distributions": dists, "itemized_deductions": itemized, "notes": notes}
        rec = build_erp_record(BANK, raw)
        rec["meta"].update(meta)
        rec["meta"]["rate_source"] = "sheet"
        records.append(rec)

    # the sheet's own totals row must agree with the bills we read, or a bill was missed
    for r in range(last_row + 1, min(last_row + 12, ws.max_row) + 1):
        tb, tc = _num(ws.cell(r, amount_col).value), _num(ws.cell(r, proceeds_col).value)
        if not ws.cell(r, 1).value and tb is not None and tc is not None:
            got_b = sum((D(str(x["bill_invoice_amount"])) for x in records), D("0"))
            got_c = sum((D(str(x["meta"]["proceeds"])) for x in records), D("0"))
            if abs(got_b - tb) > D("0.005") or abs(got_c - tc) > D("0.005"):
                for x in records:
                    x["issues"].append(f"sheet totals ({tb} / {tc}) differ from the bills read ({got_b} / {got_c}): "
                                       "a bill row may have been skipped")
                    x["needs_review"] = True
            break
    return records


def parse_workbook(path: Path | str) -> list[dict]:
    """ERP records for every bill in every EBL-style sheet. ValueError if the file is not one."""
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=False)
    records = []
    for ws in wb.worksheets:
        records.extend(_parse_sheet(ws))
    if not records:
        raise ValueError("no 'BILL NO.' / 'PROCEEDS' table with bill rows found")
    name = Path(path).name
    for rec in records:
        rec.update(source_file=name, extractor="rules")
    return records
