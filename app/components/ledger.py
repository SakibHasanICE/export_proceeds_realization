"""Local Excel register: one row per stored bill, filled when the bank mail arrives.

Columns: Serial No | Date | the ERP form fields | Deductions at Source | one column per distribution
account number (the header IS the account number, the cell is the document-currency amount).
Grand totals / the cumulative balance are deliberately not stored.

The register is derived from the database, never from the pipeline's in-memory state, so:
  * if Excel has the file open (write refused) nothing is lost: the next sync appends what is missing;
  * deleting the file rebuilds it completely on the next sync;
  * a bill is appended once (matched on the Bill/Invoice No column), never twice.
Edits made to an entry in the UI after it was stored are not copied into rows already written.
"""
import os
from datetime import date, datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.common.logger import get_logger
from app.components import database
from app.config.settings import settings

log = get_logger(__name__)

FIXED = ["Serial No", "Date", "Bank", "Beneficiary", "Buyer", "LC/SC No", "Received Date", "Bill/Invoice Date",
         "Bill/Invoice No", "Currency", "Bill/Invoice Amount", "Conversion Rate", "Deductions at Source"]
UNMAPPED = "Unmapped account"          # last resort: account number unknown AND the bank's printed reference unavailable
DATE_COLS = {"Date", "Received Date", "Bill/Invoice Date"}
WIDTHS = {"Serial No": 10, "Date": 13, "Bank": 9, "Beneficiary": 34, "Buyer": 34, "LC/SC No": 20, "Received Date": 15,
          "Bill/Invoice Date": 17, "Bill/Invoice No": 20, "Currency": 10, "Bill/Invoice Amount": 20,
          "Conversion Rate": 16, "Deductions at Source": 21}
HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="1F4E78")


TEXT_COLS = {"Bank", "Beneficiary", "Buyer", "LC/SC No", "Bill/Invoice No", "Currency"}


def _header_width(name: str) -> float:
    """Column width that always shows the whole header: bold digits are wider than the width unit, so the
    16-digit account numbers need ~25 characters, not 16."""
    return max(WIDTHS.get(name, 0), len(name) * 1.3 + 4)


def _style_header(cell) -> None:
    # Text format ("@") is essential: account numbers are 12-16 digits and Excel keeps only 15 significant
    # digits of a number, so a double-click + Enter on a General-format header turns ...744004 into ...744000.
    cell.number_format = "@"
    cell.font, cell.fill = HEAD_FONT, HEAD_FILL
    # no wrap: a long unbroken number that does not quite fit would be split over two lines (last digits alone)
    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=False)


def _new_workbook() -> openpyxl.Workbook:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Export Proceeds"
    for i, h in enumerate(FIXED, 1):
        ws.cell(1, i, h)
        _style_header(ws.cell(1, i))
        ws.column_dimensions[get_column_letter(i)].width = _header_width(h)
    ws.row_dimensions[1].height = 24
    ws.freeze_panes = "C2"
    return wb


def _arrival_date(entry: dict) -> date:
    """When the bank mail arrived (stored at ingestion); legacy entries fall back to the day they were saved."""
    arrived = (entry.get("meta") or {}).get("arrived_on")
    if arrived:
        return date.fromisoformat(arrived)
    created = entry.get("created_at")
    return datetime.fromisoformat(created).date() if created else date.today()


def _iso(v) -> date | None:
    return date.fromisoformat(v) if v else None


def _sync(path: Path) -> int:
    entries = database.all_realizations()
    wb = openpyxl.load_workbook(path) if path.exists() else _new_workbook()
    ws = wb.active

    col = {}
    for c in range(1, ws.max_column + 1):
        h = ws.cell(1, c).value
        if h not in (None, ""):
            col.setdefault(str(h).strip(), c)

    def column(name: str) -> int:
        """Column of a header, appending it at the right-hand end if it does not exist yet."""
        if name not in col:
            col[name] = max(col.values(), default=0) + 1
            ws.cell(1, col[name], name)
            _style_header(ws.cell(1, col[name]))
            ws.column_dimensions[get_column_letter(col[name])].width = _header_width(name)
        return col[name]

    for h in FIXED:                                   # also repairs a header row that was edited by hand
        column(h)
    no_col, serial_col = col["Bill/Invoice No"], col["Serial No"]

    have, serial = set(), 0
    for r in range(2, ws.max_row + 1):
        v = ws.cell(r, no_col).value
        if v not in (None, ""):
            have.add(str(v).strip())
        s = ws.cell(r, serial_col).value
        if isinstance(s, (int, float)):
            serial = max(serial, int(s))
    row = max((r for r in range(2, ws.max_row + 1) if ws.cell(r, no_col).value not in (None, "")), default=1) + 1

    added = 0
    for e in entries:                                 # oldest first, so serial numbers follow arrival order
        if str(e["bill_invoice_no"]).strip() in have:
            continue
        serial += 1
        deductions = [d["document_currency"] for d in e["deductions"] if d["document_currency"] is not None]
        values = {"Serial No": serial, "Date": _arrival_date(e), "Bank": e["bank"],
                  "Beneficiary": e["beneficiary"], "Buyer": e["buyer"],
                  "LC/SC No": e["lc_sc_no"], "Received Date": _iso(e["received_date"]),
                  "Bill/Invoice Date": _iso(e["bill_invoice_date"]), "Bill/Invoice No": e["bill_invoice_no"],
                  "Currency": e["currency"], "Bill/Invoice Amount": e["bill_invoice_amount"],
                  "Conversion Rate": e["conversion_rate"],
                  "Deductions at Source": round(sum(deductions), 2) if deductions else None}
        refs = (e.get("meta") or {}).get("distribution_refs") or []
        if len(refs) != len(e["distributions"]):      # rows were added/removed in the UI: positions no longer line up
            refs = []
        for i, d in enumerate(e["distributions"]):    # header = the exact account number; same account twice = summed
            if d.get("ac_loan_no"):
                name = str(d["ac_loan_no"]).strip()
            else:                                      # number unknown (e.g. HSBC masked): use what the bank printed
                name = refs[i] if refs else UNMAPPED
            values[name] = round(values.get(name, 0) + (d["document_currency"] or 0), 2)
        for name, v in values.items():
            if v is None:
                continue
            cell = ws.cell(row, column(name), v)
            if name in DATE_COLS:
                cell.number_format = "dd-mm-yyyy"
            elif name == "Conversion Rate":
                cell.number_format = "0.00##"
            elif name in TEXT_COLS:
                cell.number_format = "@"              # an all-digit bill / LC number must stay text too
            elif name != "Serial No":
                cell.number_format = "#,##0.00"
        have.add(str(e["bill_invoice_no"]).strip())
        row += 1
        added += 1

    if added or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        wb.save(tmp)
        try:
            os.replace(tmp, path)                     # a half-written file can never replace the real one
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
    return added


def sync() -> int:
    """Append every stored bill that is not in the Excel register yet. Returns the rows added.
    Disabled unless LEDGER_XLSX is set. Never raises: the register must not break ingestion."""
    path = settings.ledger_path
    if not path:
        return 0
    try:
        added = _sync(Path(path))
        if added:
            log.info("Excel register %s: %s row(s) added", path, added)
        return added
    except PermissionError:
        log.warning("Excel register %s is open in Excel (or read-only): close it, the rows are added next time", path)
    except Exception:
        log.exception("Excel register update failed (the database is unaffected)")
    return 0
