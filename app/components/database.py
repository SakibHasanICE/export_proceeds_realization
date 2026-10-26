"""PostgreSQL (or SQLite for dev) storage via SQLAlchemy 2.x."""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import (JSON, Boolean, Date, DateTime, ForeignKey, Numeric, String,
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    distributions: Mapped[list["Distribution"]] = relationship(
        back_populates="realization", cascade="all, delete-orphan",
        order_by="Distribution.id", lazy="selectin")


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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


_engine = create_engine(settings.database_url, pool_pre_ping=True)
_Session = sessionmaker(_engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)


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


def _to_dict(r: Realization) -> dict:
    doc_total = sum((Decimal(str(d.document_currency)) for d in r.distributions), Decimal("0"))
    balance = (Decimal(str(r.bill_invoice_amount)) - doc_total) if r.bill_invoice_amount is not None else None
    return {
        "grand_total_document": _f(doc_total),
        "cumulative_bill_balance": _f(balance),     # bill amount - distributions; must be 0 to approve
        "grand_total_domestic": _f(r.total_domestic),
        "id": r.id, "bank": r.bank, "bill_invoice_no": r.bill_invoice_no,
        "beneficiary": r.beneficiary, "buyer": r.buyer, "lc_sc_no": r.lc_sc_no,
        "received_date": r.received_date.isoformat() if r.received_date else None,
        "bill_invoice_date": r.bill_invoice_date.isoformat() if r.bill_invoice_date else None,
        "currency": r.currency, "bill_invoice_amount": _f(r.bill_invoice_amount),
        "conversion_rate": _f(r.conversion_rate), "total_domestic": _f(r.total_domestic),
        "status": r.status, "needs_review": r.needs_review, "issues": r.issues or [],
        "notes": r.notes or [], "meta": r.meta or {}, "source_file": r.source_file,
        "created_at": r.created_at.isoformat() if r.created_at else None,
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
def save_realization(rec: dict, message_id: str | None = None) -> tuple[str, int]:
    """Returns ('saved'|'duplicate', id). One transaction: all rows commit together or not at all."""
    try:
        with get_session() as s:
            existing = s.scalar(select(Realization).where(
                Realization.bank == rec["bank"], Realization.bill_invoice_no == rec["bill_invoice_no"]))
            if existing:
                return "duplicate", existing.id
            r = Realization(
                bank=rec["bank"], bill_invoice_no=rec["bill_invoice_no"], beneficiary=rec["beneficiary"],
                received_date=date.fromisoformat(rec["received_date"]),
                bill_invoice_date=date.fromisoformat(rec["bill_invoice_date"]),
                currency=rec["currency"], bill_invoice_amount=_q2(rec["bill_invoice_amount"]),
                conversion_rate=Decimal(str(rec["conversion_rate"])) if rec["conversion_rate"] is not None else None,
                total_domestic=_q2(rec["total_domestic"]), needs_review=rec["needs_review"],
                issues=rec["issues"], notes=rec["notes"], meta=rec["meta"],
                source_file=rec.get("source_file"), message_id=message_id,
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


def save_other_document(rec: dict, message_id: str | None = None) -> int:
    with get_session() as s:
        doc = OtherDocument(
            source_file=rec.get("source_file"), bank=rec.get("bank"), doc_type=rec.get("doc_type"),
            extractor=rec.get("extractor"), message_id=message_id,
            status="needs_review" if rec.get("extractor") == "llm_fallback" else "unsupported",
            payload={k: v for k, v in rec.items() if k not in ("source_file",)})
        s.add(doc)
        s.commit()
        return doc.id


# ---------------------------------------------------------------- read
def list_realizations(status: str | None = None, needs_review: bool | None = None, limit: int = 200) -> list[dict]:
    with get_session() as s:
        q = select(Realization).order_by(Realization.id.desc()).limit(limit)
        if status:
            q = q.where(Realization.status == status)
        if needs_review is not None:
            q = q.where(Realization.needs_review == needs_review)
        return [_to_dict(r) for r in s.scalars(q)]


def get_realization(rid: int) -> dict | None:
    with get_session() as s:
        r = s.get(Realization, rid)
        return _to_dict(r) if r else None


def list_other_documents(limit: int = 200) -> list[dict]:
    with get_session() as s:
        rows = s.scalars(select(OtherDocument).order_by(OtherDocument.id.desc()).limit(limit))
        return [{"id": d.id, "source_file": d.source_file, "bank": d.bank, "doc_type": d.doc_type,
                 "extractor": d.extractor, "status": d.status, "payload": d.payload,
                 "created_at": d.created_at.isoformat()} for d in rows]


# ---------------------------------------------------------------- edit / approve
def update_realization(rid: int, fields: dict, distributions: list[dict] | None = None) -> dict | None:
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
        total = Decimal("0")
        for d in r.distributions:
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


def erp_payload(d: dict) -> dict:
    """Shaped like the ERP 'Export Proceeds Realization Entry' form (dd-mm-yyyy dates)."""
    fmt = lambda iso: date.fromisoformat(iso).strftime("%d-%m-%Y") if iso else None
    return {
        "Beneficiary": d["beneficiary"], "Buyer": d["buyer"], "Bill/Invoice No": d["bill_invoice_no"],
        "Received Date": fmt(d["received_date"]), "LC/SC No": d["lc_sc_no"], "Currency": d["currency"],
        "Bill/Invoice Date": fmt(d["bill_invoice_date"]), "Bill/Invoice Amount": d["bill_invoice_amount"],
        "Distributions": [{"Account Head": x["account_head"], "AC / Loan No": x["ac_loan_no"],
                           "Document Currency": x["document_currency"],
                           "Conversion Rate": x["conversion_rate"],
                           "Domestic Currency": x["domestic_currency"]} for x in d["distributions"]],
    }
