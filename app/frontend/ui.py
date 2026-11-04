"""Streamlit review UI. Run: streamlit run app/frontend/ui.py (the API must be running)."""
import os
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pandas as pd
import requests
import streamlit as st

API_URL = os.getenv("API_URL", f"http://localhost:{os.getenv('API_PORT', '8000')}")
HEADERS = {"X-API-Key": os.getenv("API_KEY")} if os.getenv("API_KEY") else {}


def dec(x) -> Decimal:
    return Decimal(str(x))


def to_df(rows: list, cols: list) -> pd.DataFrame:
    return pd.DataFrame(rows)[cols] if rows else pd.DataFrame(columns=cols)


def totals(df: pd.DataFrame, rate):
    """(document total, domestic total) of a table; domestic = document x rate, rounded per row."""
    docs = [dec(v) for v in df["document_currency"].dropna()]
    doc = sum(docs, Decimal("0"))
    dom = sum(((d * dec(rate)).quantize(Decimal("0.01"), ROUND_HALF_UP) for d in docs), Decimal("0")) if rate else None
    return doc, dom


def clean(v):
    """Data-editor cells come back as NaN/'' when empty."""
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "" else str(v).strip()


def api(method: str, path: str, **kw):
    r = requests.request(method, f"{API_URL}{path}", headers=HEADERS, timeout=180, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(detail)
    return r.json()


st.set_page_config(page_title="Export Proceeds Realization", layout="wide")
st.title("Export Proceeds Realization")

with st.sidebar:
    st.header("Mailbox")
    if st.button("Poll mailbox now", use_container_width=True):
        try:
            with st.spinner("Checking mailbox..."):
                st.success(api("POST", "/poll"))
        except Exception as e:
            st.error(str(e))
    status_filter = st.selectbox("Status", ["all", "pending", "approved"])
    review_only = st.checkbox("Needs review only")

tab_entries, tab_upload, tab_other = st.tabs(["Entries", "Manual upload", "Other documents"])

with tab_entries:
    params = {}
    if status_filter != "all":
        params["status"] = status_filter
    if review_only:
        params["needs_review"] = "true"
    try:
        items = api("GET", "/realizations", params=params)
    except Exception as e:
        st.error(f"API error: {e}")
        items = []

    if not items:
        st.info("No entries yet.")
    else:
        st.dataframe(pd.DataFrame([{
            "id": i["id"], "bank": i["bank"], "bill/invoice no": i["bill_invoice_no"],
            "beneficiary": i["beneficiary"], "received": i["received_date"], "ccy": i["currency"],
            "amount": i["bill_invoice_amount"], "rate": i["conversion_rate"],
            "status": i["status"], "review": "YES" if i["needs_review"] else ""} for i in items]),
            use_container_width=True, hide_index=True)

        labels = {i["id"]: f'#{i["id"]}  {i["bank"]}  {i["bill_invoice_no"]}  {i["beneficiary"]}' for i in items}
        sel = st.selectbox("Open entry", list(labels), format_func=labels.get)
        rec = api("GET", f"/realizations/{sel}")
        editable = rec["status"] == "pending"

        for msg in rec["issues"]:
            st.error(msg)
        for msg in rec["notes"]:
            st.info(msg)

        c1, c2, c3 = st.columns(3)
        beneficiary = c1.text_input("Beneficiary", rec["beneficiary"] or "", key=f"b{sel}", disabled=not editable)
        buyer = c2.text_input("Buyer", rec["buyer"] or "", key=f"by{sel}", disabled=not editable)
        lc_sc = c3.text_input("LC/SC No", rec["lc_sc_no"] or "", key=f"lc{sel}", disabled=not editable)
        c4, c5, c6, c7, c8 = st.columns(5)
        received = c4.date_input("Received date", date.fromisoformat(rec["received_date"]),
                                 key=f"rd{sel}", disabled=not editable)
        bill_date = c5.date_input("Bill/Invoice date", date.fromisoformat(rec["bill_invoice_date"] or rec["received_date"]),
                                  key=f"bd{sel}", disabled=not editable)
        c6.text_input("Bill/Invoice No", rec["bill_invoice_no"], disabled=True, key=f"bn{sel}")
        c7.text_input("Currency", rec["currency"], disabled=True, key=f"cu{sel}")
        amount = c8.number_input("Bill/Invoice amount", value=float(rec["bill_invoice_amount"] or 0),
                                 format="%.2f", key=f"am{sel}", disabled=not editable)
        rate = st.number_input("Conversion rate (domestic / document)", value=float(rec["conversion_rate"] or 0),
                               format="%.4f", step=0.01, key=f"rt{sel}", disabled=not editable)

        ro = {"disabled": not editable}
        calc = st.column_config.NumberColumn(disabled=True, help="calculated = document x rate when you save")

        st.subheader("Deductions at source")
        ded_edit = st.data_editor(
            to_df(rec["deductions"], ["label", "account_head", "document_currency", "conversion_rate", "domestic_currency"]),
            num_rows="dynamic", use_container_width=True, key=f"ded{sel}",
            column_config={"conversion_rate": calc, "domestic_currency": calc}, **ro)
        ded_doc, ded_dom = totals(ded_edit, rate)
        st.caption(f"Total: document {ded_doc:,.4f}  |  domestic {ded_dom:,.2f}" if ded_dom is not None
                   else f"Total: document {ded_doc:,.4f}")

        st.subheader("Distributions")
        dist_edit = st.data_editor(
            to_df(rec["distributions"], ["account_head", "ac_loan_no", "document_currency", "conversion_rate", "domestic_currency"]),
            num_rows="dynamic", use_container_width=True, key=f"dist{sel}",
            column_config={"conversion_rate": calc, "domestic_currency": calc}, **ro)
        dist_doc, dist_dom = totals(dist_edit, rate)
        st.caption(f"Total: document {dist_doc:,.4f}  |  domestic {dist_dom:,.2f}" if dist_dom is not None
                   else f"Total: document {dist_doc:,.4f}")

        # ---- Grand total (live preview from the values currently on screen) -------------------
        grand_doc = ded_doc + dist_doc
        grand_dom = (ded_dom + dist_dom) if rate else None
        balance = dec(amount) - grand_doc
        g1, g2, g3 = st.columns(3)
        g1.metric("Grand total: document currency", f"{grand_doc:,.4f}")
        g2.metric("Cumulative bill balance", f"{balance:,.5f}")
        g3.metric("Grand total: domestic currency", f"{grand_dom:,.4f}" if grand_dom is not None else "-")
        if balance == 0:
            st.success("Balance is 0: bill amount equals deductions + distributions.")
        else:
            st.error(f"Balance is {balance:,.5f}. It must be 0: bill amount ({dec(amount):,.2f}) minus "
                     f"deductions ({ded_doc:,.2f}) and distributions ({dist_doc:,.2f}).")
        st.caption("These totals update as you edit. Click Save to store them, then Approve.")

        if rec["missing_for_erp"]:
            st.warning("Still needed before approval: " + "; ".join(rec["missing_for_erp"]))

        b1, b2, _ = st.columns([1, 1, 4])
        if b1.button("Save", disabled=not editable, key=f"save{sel}"):
            body = {"beneficiary": beneficiary, "buyer": buyer or None, "lc_sc_no": lc_sc or None,
                    "received_date": received.isoformat(), "bill_invoice_date": bill_date.isoformat(),
                    "bill_invoice_amount": amount, "conversion_rate": rate or None,
                    "deductions": [{"label": clean(r.get("label")), "account_head": clean(r.get("account_head")),
                                    "document_currency": float(r["document_currency"])}
                                   for r in ded_edit.to_dict("records") if pd.notna(r.get("document_currency"))],
                    "distributions": [{"account_head": clean(r.get("account_head")),
                                       "ac_loan_no": clean(r.get("ac_loan_no")),
                                       "document_currency": float(r["document_currency"])}
                                      for r in dist_edit.to_dict("records") if pd.notna(r.get("document_currency"))]}
            try:
                api("PUT", f"/realizations/{sel}", json=body)
                st.success("Saved")
                st.rerun()
            except Exception as e:
                st.error(str(e))
        if b2.button("Approve", disabled=not editable or bool(rec["missing_for_erp"]), type="primary", key=f"ap{sel}"):
            try:
                api("POST", f"/realizations/{sel}/approve")
                st.success("Approved")
                st.rerun()
            except Exception as e:
                st.error(str(e))

        with st.expander("ERP form payload"):
            st.json(api("GET", f"/realizations/{sel}/erp"))

with tab_upload:
    up = st.file_uploader("Bank advice PDF or EBL realization sheet (.xlsx)", type=["pdf", "xlsx"])
    if up and st.button("Process file"):
        try:
            mime = "application/pdf" if up.name.lower().endswith(".pdf") else \
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            st.json(api("POST", "/upload", files={"file": (up.name, up.getvalue(), mime)}))
        except Exception as e:
            st.error(str(e))

with tab_other:
    try:
        docs = api("GET", "/other-documents")
        st.dataframe(pd.DataFrame([{k: d[k] for k in ("id", "source_file", "bank", "doc_type", "extractor", "status", "created_at")}
                                   for d in docs]), use_container_width=True, hide_index=True) if docs else st.info("None.")
    except Exception as e:
        st.error(str(e))
