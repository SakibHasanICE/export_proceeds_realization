"""PostgreSQL (or SQLite for dev) storage via SQLAlchemy 2.x."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Numeric, String, Text,
                        UniqueConstraint, create_engine, select)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, relationship,
                            sessionmaker)

from app.common.custom_exception import CustomException
from app.common.logger import get_logger
from app.config.settings import settings

log = get_logger(__name__)
CENT = Decimal("0.01")
EDITABLE = {"beneficiary", "buyer", "lc_sc_no", "received_date", "bill_invoice_date",
            "bill_invoice_amount", "conversion_rate"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _q2(x) -> Decimal:
    return Decimal(str(x)).quantize(CENT, ROUND_HALF_UP)


def _f(x):
    return float(x) if x is not None else None


class Base(DeclarativeBase):
    pass


class Realization(Base):
    __tablename__ = "export_realizations"
    __table_args__ = (UniqueConstraint("bank", "bill_invoice_no", name="uq_bank_bill"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    bank: Mapped[str] = mapped_column(String(20))
    bill_invoice_no: Mapped[str] = mapped_column(String(60))
    beneficiary: Mapped[str | None] = mapped_column(String(200))
    buyer: Mapped[str | None] = mapped_column(String(200))      # entered in the UI (not in bank PDFs)
    lc_sc_no: Mapped[str | None] = mapped_column(String(100))   # entered in the UI (not in bank PDFs)
    received_date: Mapped[date | None] = mapped_column(Date)
    bill_invoice_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str | None] = mapped_column(String(3))
    bill_invoice_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    total_domestic: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending -> approved -> filled
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    source_file: Mapped[str | None] = mapped_column(String(300))
    message_id: Mapped[str | None] = mapped_column(String(998))
    owner_email: Mapped[str | None] = mapped_column(String(320), index=True)   # who sent / uploaded it (None = old data)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    distributions: Mapped[list["Distribution"]] = relationship(
        back_populates="realization", cascade="all, delete-orphan",
        order_by="Distribution.id", lazy="selectin")
    deductions: Mapped[list["Deduction"]] = relationship(
        back_populates="realization", cascade="all, delete-orphan",
        order_by="Deduction.id", lazy="selectin")


class Distribution(Base):
    __tablename__ = "realization_distributions"

    id: Mapped[int] = mapped_column(primary_key=True)
    realization_id: Mapped[int] = mapped_column(ForeignKey("export_realizations.id", ondelete="CASCADE"))
    account_head: Mapped[str | None] = mapped_column(String(300))
    ac_loan_no: Mapped[str | None] = mapped_column(String(60))
    document_currency: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    domestic_currency: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    realization: Mapped[Realization] = relationship(back_populates="distributions")


class Deduction(Base):
    """'Deductions at Source': foreign-currency charges taken out of the bill amount."""
    __tablename__ = "realization_deductions"

    id: Mapped[int] = mapped_column(primary_key=True)
    realization_id: Mapped[int] = mapped_column(ForeignKey("export_realizations.id", ondelete="CASCADE"))
    label: Mapped[str | None] = mapped_column(String(200))        # what the bank called it (info only)
    account_head: Mapped[str | None] = mapped_column(String(300))
    document_currency: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    conversion_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    domestic_currency: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    realization: Mapped[Realization] = relationship(back_populates="deductions")


class ProcessedEmail(Base):
    __tablename__ = "processed_emails"

    message_id: Mapped[str] = mapped_column(String(998), primary_key=True)
    sender: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class OtherDocument(Base):
    """Documents that are not export payment advices (e.g. LC opening) or need LLM review."""
    __tablename__ = "other_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file: Mapped[str | None] = mapped_column(String(300))
    bank: Mapped[str | None] = mapped_column(String(20))
    doc_type: Mapped[str | None] = mapped_column(String(50))
    extractor: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="unsupported")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    message_id: Mapped[str | None] = mapped_column(String(998))
    owner_email: Mapped[str | None] = mapped_column(String(320), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ErpCredential(Base):
    """A user's own ERP login. The password is stored encrypted (components/secret_box.py), never in clear text."""
    __tablename__ = "erp_credentials"
    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    username: Mapped[str] = mapped_column(String(200))
    password_enc: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


_engine = create_engine(settings.database_url, pool_pre_ping=True)
_Session = sessionmaker(_engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)
    _add_owner_columns()


def _add_owner_columns() -> None:
    """create_all does not alter existing tables: add owner_email to databases made before personalisation."""
    from sqlalchemy import inspect, text
    insp = inspect(_engine)
    for table in ("export_realizations", "other_documents"):
        if "owner_email" in {c["name"] for c in insp.get_columns(table)}:
            continue
        with _engine.begin() as c:
            c.execute(text(f"ALTER TABLE {table} ADD COLUMN owner_email VARCHAR(320)"))
            c.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{table}_owner_email ON {table} (owner_email)"))
        log.info("added owner_email to %s", table)


@contextmanager
def get_session():
    s = _Session()
    try:
        yield s
    except Exception:
        s.rollback()   # the diagram's ROLLBACK/ALERT branch
        raise
    finally:
        s.close()


# ---------------------------------------------------------------- serialisation
def _dist_dict(d: Distribution) -> dict:
    return {"id": d.id, "account_head": d.account_head, "ac_loan_no": d.ac_loan_no,
            "document_currency": _f(d.document_currency), "conversion_rate": _f(d.conversion_rate),
            "domestic_currency": _f(d.domestic_currency)}


def _ded_dict(d: Deduction) -> dict:
    return {"id": d.id, "label": d.label, "account_head": d.account_head,
            "document_currency": _f(d.document_currency), "conversion_rate": _f(d.conversion_rate),
            "domestic_currency": _f(d.domestic_currency)}


def _to_dict(r: Realization) -> dict:
    dec = lambda rows: sum((Decimal(str(x.document_currency)) for x in rows), Decimal("0"))
    doc_total = dec(r.deductions) + dec(r.distributions)
    balance = (Decimal(str(r.bill_invoice_amount)) - doc_total) if r.bill_invoice_amount is not None else None
    return {
        "grand_total_document": _f(doc_total),       # deductions + distributions
        "cumulative_bill_balance": _f(balance),      # bill amount - grand total; must be 0 to approve
        "grand_total_domestic": _f(r.total_domestic),
        "deductions": [_ded_dict(d) for d in r.deductions],
        "id": r.id, "bank": r.bank, "bill_invoice_no": r.bill_invoice_no,
        "beneficiary": r.beneficiary, "buyer": r.buyer, "lc_sc_no": r.lc_sc_no,
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "bill_invoice_date": r.bill_invoice_date.isoformat() if r.bill_invoice_date else None,
        "currency": r.currency, "bill_invoice_amount": _f(r.bill_invoice_amount),
        "conversion_rate": _f(r.conversion_rate), "total_domestic": _f(r.total_domestic),
        "status": r.status, "needs_review": r.needs_review, "issues": r.issues or [],
        "notes": r.notes or [], "meta": r.meta or {}, "source_file": r.source_file,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "owner_email": r.owner_email,
        "distributions": [_dist_dict(d) for d in r.distributions],
    }


# ---------------------------------------------------------------- emails
def email_already_processed(message_id: str) -> bool:
    with get_session() as s:
        return s.get(ProcessedEmail, message_id) is not None


def log_email(message_id: str, sender: str, subject: str, status: str) -> None:
    with get_session() as s:
        if s.get(ProcessedEmail, message_id) is None:
            s.add(ProcessedEmail(message_id=message_id, sender=sender, subject=subject[:500], status=status))
            s.commit()


# ---------------------------------------------------------------- save
def save_realization(rec: dict, message_id: str | None = None, replace: bool = False,
                     owner_email: str | None = None) -> tuple[str, int]:
    """Returns ('saved'|'duplicate', id). One transaction: all rows commit together or not at all."""
    try:
        with get_session() as s:
            existing = s.scalar(select(Realization).where(
                Realization.bank == rec["bank"], Realization.bill_invoice_no == rec["bill_invoice_no"]))
            if existing:
                if replace and existing.status != "filled":      # re-read of an entry nobody has filled yet
                    s.delete(existing)
                    s.flush()
                else:
                    return "duplicate", existing.id
            r = Realization(
                bank=rec["bank"], bill_invoice_no=rec["bill_invoice_no"], beneficiary=rec["beneficiary"],
                received_date=date.fromisoformat(rec["received_date"]),
                bill_invoice_date=date.fromisoformat(rec["bill_invoice_date"]),
                currency=rec["currency"], bill_invoice_amount=_q2(rec["bill_invoice_amount"]),
                conversion_rate=Decimal(str(rec["conversion_rate"])) if rec["conversion_rate"] is not None else None,
                buyer=rec.get("buyer"), lc_sc_no=rec.get("lc_sc_no"),
                total_domestic=_q2(rec["total_domestic"]) if rec["total_domestic"] is not None else None,
                needs_review=rec["needs_review"],
                issues=rec["issues"], notes=rec["notes"], meta=rec["meta"],
                source_file=rec.get("source_file"), message_id=message_id, owner_email=owner_email,
                deductions=[Deduction(
                    label=d.get("label"), account_head=d.get("account_head"),
                    document_currency=_q2(d["document_currency"]),
                    conversion_rate=Decimal(str(d["conversion_rate"])) if d["conversion_rate"] is not None else None,
                    domestic_currency=_q2(d["domestic_currency"]) if d["domestic_currency"] is not None else None,
                ) for d in rec.get("deductions", [])],
                distributions=[Distribution(
                    account_head=d["account_head"], ac_loan_no=d["ac_loan_no"],
                    document_currency=_q2(d["document_currency"]),
                    conversion_rate=Decimal(str(d["conversion_rate"])) if d["conversion_rate"] is not None else None,
                    domestic_currency=_q2(d["domestic_currency"]) if d["domestic_currency"] is not None else None,
                ) for d in rec["distributions"]])
            s.add(r)
            try:
                s.commit()
            except IntegrityError:   # race with another cycle: treat as duplicate
                s.rollback()
                dup = s.scalar(select(Realization).where(
                    Realization.bank == rec["bank"], Realization.bill_invoice_no == rec["bill_invoice_no"]))
                return "duplicate", dup.id
            return "saved", r.id
    except Exception as e:
        log.error("save_realization failed for %s: %s", rec.get("bill_invoice_no"), e)
        raise CustomException("Could not save realization", e)


def save_other_document(rec: dict, message_id: str | None = None, owner_email: str | None = None) -> int:
    with get_session() as s:
        doc = OtherDocument(
            source_file=rec.get("source_file"), bank=rec.get("bank"), doc_type=rec.get("doc_type"),
            extractor=rec.get("extractor"), message_id=message_id, owner_email=owner_email,
            status="needs_review" if rec.get("extractor") == "llm_fallback" else "unsupported",
            payload={k: v for k, v in rec.items() if k not in ("source_file",)})
        s.add(doc)
        s.commit()
        return doc.id


# ---------------------------------------------------------------- read
def list_realizations(status: str | None = None, needs_review: bool | None = None, limit: int = 200,
                      owner_email: str | None = None) -> list[dict]:
    """owner_email=None -> everything (master / sign-in off); otherwise only that user's own entries."""
    with get_session() as s:
        q = select(Realization).order_by(Realization.id.desc()).limit(limit)
        if owner_email is not None:
            q = q.where(Realization.owner_email == owner_email)
        if status:
            q = q.where(Realization.status == status)
        if needs_review is not None:
            q = q.where(Realization.needs_review == needs_review)
        return [_to_dict(r) for r in s.scalars(q)]


def all_realizations() -> list[dict]:
    """Every entry, oldest first, with deductions and distributions (feeds the Excel register)."""
    with get_session() as s:
        return [_to_dict(r) for r in s.scalars(select(Realization).order_by(Realization.id))]


def get_realization(rid: int) -> dict | None:
    with get_session() as s:
        r = s.get(Realization, rid)
        return _to_dict(r) if r else None


def list_other_documents(limit: int = 200, owner_email: str | None = None) -> list[dict]:
    with get_session() as s:
        q = select(OtherDocument).order_by(OtherDocument.id.desc()).limit(limit)
        if owner_email is not None:
            q = q.where(OtherDocument.owner_email == owner_email)
        rows = s.scalars(q)
        return [{"id": d.id, "owner_email": d.owner_email, "source_file": d.source_file, "bank": d.bank, "doc_type": d.doc_type,
                 "extractor": d.extractor, "status": d.status, "payload": d.payload,
                 "created_at": d.created_at.isoformat()} for d in rows]


# ---------------------------------------------------------------- edit / approve
def update_realization(rid: int, fields: dict, distributions: list[dict] | None = None,
                       deductions: list[dict] | None = None) -> dict | None:
    """Apply user edits, then recompute domestic = document x rate for every row."""
    with get_session() as s:
        r = s.get(Realization, rid)
        if r is None:
            return None
        if r.status != "pending":
            raise ValueError(f"entry is {r.status}; only pending entries can be edited")
        for k, v in fields.items():
            if k not in EDITABLE:
                continue
            if k == "bill_invoice_amount" and v is not None:
                v = _q2(v)
            elif k == "conversion_rate" and v is not None:
                v = Decimal(str(v))
            setattr(r, k, v)
        if distributions is not None:
            r.distributions.clear()
            s.flush()
            for d in distributions:
                r.distributions.append(Distribution(
                    account_head=d.get("account_head"), ac_loan_no=d.get("ac_loan_no"),
                    document_currency=_q2(d["document_currency"])))
        if deductions is not None:
            r.deductions.clear()
            s.flush()
            for d in deductions:
                r.deductions.append(Deduction(
                    label=d.get("label"), account_head=d.get("account_head"),
                    document_currency=_q2(d["document_currency"])))
        total = Decimal("0")
        for d in list(r.deductions) + list(r.distributions):
            if r.conversion_rate is not None:
                d.conversion_rate = r.conversion_rate
                d.domestic_currency = _q2(Decimal(str(d.document_currency)) * Decimal(str(r.conversion_rate)))
                total += d.domestic_currency
            else:
                d.conversion_rate = d.domestic_currency = None
        r.total_domestic = total if r.conversion_rate is not None else None
        r.updated_at = _now()
        s.commit()
        return _to_dict(r)


def missing_for_erp(d: dict) -> list[str]:
    """Everything the ERP form needs that is still empty or inconsistent."""
    missing = [f for f in ("beneficiary", "buyer", "lc_sc_no", "bill_invoice_no", "received_date",
                           "currency", "bill_invoice_amount", "conversion_rate") if not d.get(f)]
    if not d["distributions"]:
        missing.append("distributions")
    for i, row in enumerate(d["distributions"], 1):
        for f in ("account_head", "ac_loan_no", "domestic_currency"):
            if not row.get(f):
                missing.append(f"distribution {i}: {f}")
    for i, row in enumerate(d.get("deductions", []), 1):
        for f in ("account_head", "domestic_currency"):
            if not row.get(f):
                missing.append(f"deduction {i}: {f}")
    bal = d.get("cumulative_bill_balance")
    if d["distributions"] and bal is not None and abs(bal) > 0.005:
        missing.append(f"cumulative bill balance is {bal:.2f} (must be 0)")
    return missing


def approve_realization(rid: int) -> dict | None:
    with get_session() as s:
        r = s.get(Realization, rid)
        if r is None:
            return None
        missing = missing_for_erp(_to_dict(r))
        if missing:
            raise ValueError("; ".join(missing))
        r.status = "approved"
        r.updated_at = _now()
        s.commit()
        return _to_dict(r)


def mark_filled(rid: int, note: str | None = None) -> dict | None:
    """The RPA robot reports it has typed an approved entry into the ERP. Only approved entries qualify.
    Calling it again on an entry that is already filled changes nothing, so the robot can retry safely."""
    with get_session() as s:
        r = s.get(Realization, rid)
        if r is None:
            return None
        if r.status == "filled":
            return _to_dict(r)
        if r.status != "approved":
            raise ValueError(f"entry is {r.status}; only approved entries can be marked filled")
        r.status = "filled"
        r.meta = {**(r.meta or {}), "erp_fill": {"at": _now().isoformat(), "note": note}}
        r.updated_at = _now()
        s.commit()
        return _to_dict(r)


def _save_log(meta: dict, state: str | None, note: str | None, by: str | None) -> list:
    log = list(meta.get("erp_save_log") or [])[-19:]
    log.append({"at": _now().isoformat(), "state": state or "cleared", "by": by, "note": note})
    return log


def set_erp_save_state(rid: int, state: str | None, note: str | None = None, by: str | None = None) -> dict | None:
    """What happened at the ERP's Save button for this entry (kept in meta, so no migration is needed).
        in_progress  Save is about to be / has just been clicked
        unconfirmed  Save was clicked but the ERP did not clearly answer: a person must look in the ERP
        None         cleared: the ERP refused, or a person confirmed it was NOT saved
    While a state is set the entry cannot be run again, which is what prevents a double save."""
    with get_session() as s:
        r = s.get(Realization, rid)
        if r is None:
            return None
        meta = dict(r.meta or {})
        if state:
            meta["erp_save_state"], meta["erp_save_at"] = state, _now().isoformat()
        else:
            meta.pop("erp_save_state", None)
            meta.pop("erp_save_at", None)
        meta["erp_save_log"] = _save_log(meta, state, note, by)
        r.meta = meta
        s.commit()
        return _to_dict(r)


def mark_saved_in_erp(rid: int, note: str | None = None, by: str | None = None) -> dict | None:
    """The entry IS saved in the ERP (the robot read the ERP's answer, or a person confirmed it). Final: status filled.
    Never refuses: the ERP already holds the entry, so the database must say so whatever else is true."""
    with get_session() as s:
        r = s.get(Realization, rid)
        if r is None:
            return None
        meta = dict(r.meta or {})
        meta.pop("erp_save_state", None)
        meta.pop("erp_save_at", None)
        meta["erp_save_log"] = _save_log(meta, "saved", note, by)
        meta["erp_fill"] = {"at": _now().isoformat(), "note": note, "by": by}
        r.meta = meta
        r.status = "filled"
        r.updated_at = _now()
        s.commit()
        return _to_dict(r)


def record_erp_click(rid: int, user: str | None) -> None:
    """Someone pressed Proceed to ERP / Run again for this entry: remember when and who (last click + a short log).
    Kept inside meta (JSON), so existing databases need no migration."""
    with get_session() as s:
        r = s.get(Realization, rid)
        if r is None:
            return
        now = _now().isoformat()
        meta = dict(r.meta or {})
        log = list(meta.get("erp_click_log") or [])[-19:]
        log.append({"at": now, "by": user})
        meta.update({"erp_clicked_at": now, "erp_clicked_by": user, "erp_click_log": log})
        r.meta = meta          # new dict, so SQLAlchemy sees the change
        s.commit()


def erp_payload(d: dict) -> dict:
    """Shaped like the ERP 'Export Proceeds Realization Entry' form (dd-mm-yyyy dates)."""
    fmt = lambda iso: date.fromisoformat(iso).strftime("%d-%m-%Y") if iso else None
    return {
        "Beneficiary": d["beneficiary"], "Buyer": d["buyer"], "Bill/Invoice No": d["bill_invoice_no"],
        "Received Date": fmt(d["received_date"]), "LC/SC No": d["lc_sc_no"], "Currency": d["currency"],
        "Bill/Invoice Date": fmt(d["bill_invoice_date"]), "Bill/Invoice Amount": d["bill_invoice_amount"],
        "Deductions at Source": [{"Account Head": x["account_head"], "Document Currency": x["document_currency"],
                                  "Conversion Rate": x["conversion_rate"],
                                  "Domestic Currency": x["domestic_currency"]} for x in d["deductions"]],
        "Distributions": [{"Account Head": x["account_head"], "AC / Loan No": x["ac_loan_no"],
                           "Document Currency": x["document_currency"],
                           "Conversion Rate": x["conversion_rate"],
                           "Domestic Currency": x["domestic_currency"]} for x in d["distributions"]],
        "Grand Total": {"Document Currency": d["grand_total_document"],
                        "Cumulative Bill Balance": d["cumulative_bill_balance"],
                        "Domestic Currency": d["grand_total_domestic"]},
    }


# ---------------------------------------------------------------- personal ERP logins
def save_erp_credential(email: str, username: str, password_enc: str) -> None:
    with get_session() as s:
        row = s.get(ErpCredential, email.lower())
        if row is None:
            s.add(ErpCredential(email=email.lower(), username=username, password_enc=password_enc))
        else:
            row.username, row.password_enc, row.updated_at = username, password_enc, _now()
        s.commit()


def get_erp_credential(email: str) -> tuple[str, str] | None:
    """(username, encrypted password) or None."""
    with get_session() as s:
        row = s.get(ErpCredential, email.lower())
        return (row.username, row.password_enc) if row else None


def delete_erp_credential(email: str) -> bool:
    with get_session() as s:
        row = s.get(ErpCredential, email.lower())
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


# ---------------------------------------------------------------- paginated list (the web page: 100 per page)
def _entry_filters(owner_email, status, search, owner_filter):
    from sqlalchemy import cast, or_
    conds = []
    if owner_email is not None:                     # a normal user: only their own
        conds.append(Realization.owner_email == owner_email)
    elif owner_filter == "none":                    # master picked "no owner"
        conds.append(Realization.owner_email.is_(None))
    elif owner_filter and owner_filter != "all":    # master picked one user
        conds.append(Realization.owner_email == owner_filter.lower())
    if status and status != "all":
        conds.append(Realization.status == status)
    q = (search or "").strip()
    if q:
        pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        cols = [cast(Realization.id, String), Realization.bank, Realization.bill_invoice_no, Realization.beneficiary,
                Realization.buyer, Realization.lc_sc_no, Realization.owner_email]
        conds.append(or_(*[c.ilike(pat, escape="\\") for c in cols]))
    return conds


def query_realizations(owner_email: str | None = None, status: str | None = None, search: str | None = None,
                       owner_filter: str | None = None, offset: int = 0, limit: int | None = None) -> tuple[list[dict], int]:
    """(rows newest first, total matching). limit=None returns every match (used when the 'ready' filter needs them all)."""
    from sqlalchemy import func
    conds = _entry_filters(owner_email, status, search, owner_filter)
    with get_session() as s:
        total = s.scalar(select(func.count()).select_from(Realization).where(*conds)) or 0
        q = select(Realization).where(*conds).order_by(Realization.id.desc()).offset(offset)
        if limit is not None:
            q = q.limit(limit)
        return [_to_dict(r) for r in s.scalars(q)], total


def distinct_owners() -> list[str]:
    with get_session() as s:
        return sorted(o for o in s.scalars(select(Realization.owner_email).distinct()) if o)


# ---------------------------------------------------------------- user edits of one value ("edited" is remembered)
_EDIT_TEXT = {"beneficiary", "buyer", "lc_sc_no", "bill_invoice_no"}
_EDIT_DATES = {"received_date", "bill_invoice_date"}


def _amount(value, what: str, allow_zero: bool = False) -> Decimal:
    try:
        d = Decimal(str(value).replace(",", "").strip())
    except Exception:
        raise ValueError(f"{what}: '{value}' is not a number")
    if d < 0 or (d == 0 and not allow_zero):
        raise ValueError(f"{what} must be {'zero or more' if allow_zero else 'more than zero'}")
    return d


def _fmt_rate(v) -> str:
    return "" if v is None else format(Decimal(str(v)).normalize(), "f")


def edit_entry(rid: int, field: str, value, user: str | None = None) -> dict | None:
    """Change ONE value of a pending entry the way the user typed it, and remember that it was edited.

    meta['edited'][field] = {original, by, at}: the ORIGINAL (processed) value is kept from the first edit on, and
    the mark disappears again when the value is put back to it. field: a column name, 'deductions' (when the entry
    has exactly one deduction line) or 'dist:<account no>' (an existing distribution line; 0 removes the line)."""
    with get_session() as s:
        r = s.get(Realization, rid)
        if r is None:
            return None
        if r.status != "pending":
            raise ValueError(f"This entry is {r.status}: only pending entries can be edited.")
        if field in _EDIT_TEXT:
            new = (str(value).strip() if value is not None else "") or None
            if field == "bill_invoice_no" and not new:
                raise ValueError("Bill/Invoice No cannot be empty.")
            before, after = getattr(r, field) or "", new or ""
            setattr(r, field, new)
        elif field in _EDIT_DATES:
            try:
                new = date.fromisoformat(str(value).strip()[:10]) if value not in (None, "") else None
            except ValueError:
                raise ValueError("Enter the date as day-month-year.")
            before = getattr(r, field).isoformat() if getattr(r, field) else ""
            after = new.isoformat() if new else ""
            setattr(r, field, new)
        elif field == "bill_invoice_amount":
            new = _q2(_amount(value, "Bill Amount"))
            before = f"{_q2(r.bill_invoice_amount):.2f}" if r.bill_invoice_amount is not None else ""
            after = f"{new:.2f}"
            r.bill_invoice_amount = new
        elif field == "conversion_rate":
            new = _amount(value, "Rate")
            before, after = _fmt_rate(r.conversion_rate), _fmt_rate(new)
            r.conversion_rate = new
        elif field == "deductions":
            rows = list(r.deductions)
            if len(rows) != 1:
                raise ValueError("This entry has " + ("no" if not rows else "several") +
                                 " deduction lines, so the total cannot be edited as one value.")
            new = _q2(_amount(value, "Deductions", allow_zero=True))
            before, after = f"{_q2(rows[0].document_currency):.2f}", f"{new:.2f}"
            rows[0].document_currency = new
        elif field.startswith("dist:"):
            ac = field[5:]
            rows = [d for d in r.distributions if d.ac_loan_no == ac]
            if len(rows) != 1:
                raise ValueError("Only an account with exactly one distribution line can be edited here.")
            new = _q2(_amount(value, "Amount", allow_zero=True))
            before, after = f"{_q2(rows[0].document_currency):.2f}", f"{new:.2f}"
            if new == 0:
                r.distributions.remove(rows[0])
            else:
                rows[0].document_currency = new
        else:
            raise ValueError("This value cannot be edited.")

        meta = dict(r.meta or {})
        edited = dict(meta.get("edited") or {})
        original = (edited.get(field) or {}).get("original", before)
        if after == original:
            edited.pop(field, None)                      # put back to the processed value: no longer "edited"
        else:
            edited[field] = {"original": original, "by": user, "at": _now().isoformat()}
        if edited:
            meta["edited"] = edited
        else:
            meta.pop("edited", None)
        r.meta = meta

        total = Decimal("0")                              # domestic = document x rate, for every line
        for d in list(r.deductions) + list(r.distributions):
            if r.conversion_rate is not None:
                d.conversion_rate = r.conversion_rate
                d.domestic_currency = _q2(Decimal(str(d.document_currency)) * Decimal(str(r.conversion_rate)))
                total += d.domestic_currency
            else:
                d.conversion_rate = d.domestic_currency = None
        r.total_domestic = total if r.conversion_rate is not None else None
        r.updated_at = _now()
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise ValueError("Another entry of this bank already has this Bill/Invoice No.")
        log.info("entry %s: %s edited by %s (%r -> %r)", rid, field, user, before, after)
        return _to_dict(r)
