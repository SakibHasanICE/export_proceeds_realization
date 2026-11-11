"""FastAPI backend: review, edit and approve extracted realizations."""
import hmac
import uuid
from contextlib import asynccontextmanager
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel

from app.common.custom_exception import CustomException
from app.common.logger import get_logger
from app.backend.erp_api import build_router as build_erp_router
from app.components import database, pipeline
from app.config.settings import settings

log = get_logger(__name__)
MAX_UPLOAD = 10 * 1024 * 1024


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("database: %s", settings.database_url.split("@")[-1])   # no password in the log
    database.init_db()
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    if settings.scheduler_enabled:
        pipeline.start_scheduler()
    yield
    pipeline.stop_scheduler()


def require_key(x_api_key: Optional[str] = Header(default=None)):
    if settings.api_key and not hmac.compare_digest(x_api_key or "", settings.api_key):
        raise HTTPException(401, "invalid or missing API key")


app = FastAPI(title="Export Proceeds Realization Automation", lifespan=lifespan)
router = APIRouter(dependencies=[Depends(require_key)])


class DistributionIn(BaseModel):
    account_head: Optional[str] = None
    ac_loan_no: Optional[str] = None
    document_currency: float


class DeductionIn(BaseModel):
    label: Optional[str] = None
    account_head: Optional[str] = None
    document_currency: float


class RealizationUpdate(BaseModel):
    beneficiary: Optional[str] = None
    buyer: Optional[str] = None
    lc_sc_no: Optional[str] = None
    received_date: Optional[date] = None
    bill_invoice_date: Optional[date] = None
    bill_invoice_amount: Optional[float] = None
    conversion_rate: Optional[float] = None
    distributions: Optional[list[DistributionIn]] = None
    deductions: Optional[list[DeductionIn]] = None


@app.get("/health")
def health():
    return {"status": "ok"}


@router.post("/poll")
def poll_now():
    """Run one mailbox cycle immediately."""
    try:
        return pipeline.run_cycle()
    except CustomException as e:
        raise HTTPException(502, str(e))


@router.post("/upload")
async def upload_pdf(file: UploadFile = File(...), replace: bool = False):
    """Manually process a bank advice PDF or an EBL realization sheet (.xlsx)."""
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "file too large (10 MB max)")
    is_pdf = data[:4] == b"%PDF"
    if not is_pdf and data[:4] != b"PK\x03\x04":      # .xlsx is a zip; the parser verifies it properly
        raise HTTPException(400, "not a PDF or Excel (.xlsx) file")
    settings.staging_dir.mkdir(parents=True, exist_ok=True)
    path = settings.staging_dir / f"upload_{uuid.uuid4().hex[:8]}{'.pdf' if is_pdf else '.xlsx'}"
    path.write_bytes(data)
    try:
        return pipeline.process_file(path, message_id=None, replace=replace)
    except CustomException as e:
        raise HTTPException(500, str(e))


@router.get("/realizations")
def list_realizations(status: Optional[str] = None, needs_review: Optional[bool] = None):
    return database.list_realizations(status, needs_review)


@router.get("/realizations/{rid}")
def get_realization(rid: int):
    rec = database.get_realization(rid)
    if rec is None:
        raise HTTPException(404, "not found")
    rec["missing_for_erp"] = database.missing_for_erp(rec)
    return rec


@router.put("/realizations/{rid}")
def update_realization(rid: int, body: RealizationUpdate):
    fields = body.model_dump(exclude_unset=True)
    dists = fields.pop("distributions", None)
    deds = fields.pop("deductions", None)
    try:
        rec = database.update_realization(rid, fields, dists, deds)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if rec is None:
        raise HTTPException(404, "not found")
    rec["missing_for_erp"] = database.missing_for_erp(rec)
    return rec


@router.post("/realizations/{rid}/approve")
def approve(rid: int):
    try:
        rec = database.approve_realization(rid)
    except ValueError as e:
        raise HTTPException(422, f"cannot approve, missing/inconsistent: {e}")
    if rec is None:
        raise HTTPException(404, "not found")
    return rec


class FilledIn(BaseModel):
    note: Optional[str] = None      # e.g. the ERP's own entry number, for the audit trail


@router.post("/realizations/{rid}/filled")
def filled(rid: int, body: Optional[FilledIn] = None):
    """RPA callback: the robot entered this approved entry into the ERP (status approved -> filled)."""
    try:
        rec = database.mark_filled(rid, body.note if body else None)
    except ValueError as e:
        raise HTTPException(409, str(e))
    if rec is None:
        raise HTTPException(404, "not found")
    return rec


@router.get("/realizations/{rid}/erp")
def erp_form(rid: int):
    """The record shaped like the ERP form (for the future auto-fill step)."""
    rec = database.get_realization(rid)
    if rec is None:
        raise HTTPException(404, "not found")
    return database.erp_payload(rec)


@router.get("/other-documents")
def other_documents():
    return database.list_other_documents()


app.include_router(router)
app.include_router(build_erp_router(require_key))
