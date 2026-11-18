"""ERP endpoints for the web UI: the table (what will be filled), and the tracked robot runs."""
import os
import re
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.common.logger import get_logger
from app.backend.auth import can_access, is_master, owner_scope
from app.components import database
from app.config.settings import settings
from app.components import secret_box
from app.erp_worker import credentials
from app.erp_worker.runner import RunManager
from app.erp_worker.save_hooks import DbSaveHooks
from app.erp_worker.view import build_erp_view, build_run_payload

log = get_logger(__name__)
SHOT_DIR = Path(os.getenv("ERP_SHOT_DIR", "erp_screens"))
RUN_DIR = Path(os.getenv("ERP_RUN_DIR", str(settings.log_dir / "erp_runs")))

_manager: Optional[RunManager] = None


def _load_payload(entry_id: int) -> dict:
    rec = database.get_realization(entry_id)
    if rec is None:
        raise ValueError(f"entry {entry_id} no longer exists")
    return build_run_payload(rec)


def get_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager(RUN_DIR, _load_payload, hooks_factory=lambda eid, user: DbSaveHooks(eid, user),
                              credential_provider=credentials.login_for)
    return _manager


class RunRequest(BaseModel):
    entry_ids: list[int] = Field(min_length=1, max_length=100)


PAGE_SIZE = 100          # entries per page


class CellEdit(BaseModel):
    field: str = Field(min_length=1, max_length=60)
    value: Optional[str] = Field(default=None, max_length=300)


class ErpLoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


class SaveResolution(BaseModel):
    resolution: Literal["saved", "not_saved"]
    note: Optional[str] = Field(default=None, max_length=300)


def save_enabled() -> bool:
    return os.getenv("ERP_SAVE_ENABLED", "false").strip().lower() == "true"


def build_router(require_key, current_user) -> APIRouter:
    router = APIRouter(prefix="/erp", dependencies=[Depends(require_key), Depends(current_user)])

    def job_ok(job_summary: Optional[dict], user: Optional[str]) -> bool:
        """A run belongs to whoever owns its entry."""
        if job_summary is None:
            return False
        rec = database.get_realization(job_summary["entry_id"])
        return rec is not None and can_access(rec.get("owner_email"), user)

    @router.get("/entries")
    def entries(page: int = 1, status: Optional[str] = None, search: str = "", owner: str = "all",
                only_ready: bool = False, user: Optional[str] = Depends(current_user)):
        """One page (100) of this user's entries, newest first. Search / status / owner filters work on ALL pages.
"""
        mgr = get_manager()
        latest = mgr.latest_by_entry()
        args = dict(owner_email=owner_scope(user), status=status, search=search, owner_filter=owner)
        page = max(1, page)

        def view(rec):
            v = build_erp_view(rec)
            v["last_run"] = latest.get(rec["id"])
            return v

        if only_ready:                       # "ready" is computed per entry, so every match is checked, then paged
            rows, _ = database.query_realizations(**args)
            ready = [v for v in map(view, rows) if v["can_run"]]
            total = len(ready)
            pages = max(1, -(-total // PAGE_SIZE))
            page = min(page, pages)
            out = ready[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
        else:
            _, total = database.query_realizations(limit=0, **args)
            pages = max(1, -(-total // PAGE_SIZE))
            page = min(page, pages)
            rows, _ = database.query_realizations(offset=(page - 1) * PAGE_SIZE, limit=PAGE_SIZE, **args)
            out = [view(r) for r in rows]
        master = is_master(user)
        owners = database.distinct_owners() if master else []
        return {"entries": out, "worker": mgr.worker_status(), "save_enabled": save_enabled(),
                "viewer": {"email": user, "is_master": master}, "owners": owners,
                "page": page, "pages": pages, "total": total, "page_size": PAGE_SIZE,
                "erp_login": credentials.mode_for(user),
                "erp_configured": bool(os.getenv("ERP_USERNAME") and os.getenv("ERP_PASSWORD"))
                                  or credentials.mode_for(user)["mode"] != "missing"}

    @router.put("/entries/{entry_id}/cell")
    def edit_cell(entry_id: int, body: CellEdit, user: Optional[str] = Depends(current_user)):
        """A user corrects one processed value. The table then shows '(edited)' under it."""
        rec = database.get_realization(entry_id)
        if rec is None or not can_access(rec.get("owner_email"), user):
            raise HTTPException(404, "entry not found")
        if get_manager().active_for_entry(entry_id):
            raise HTTPException(409, "A run is in progress for this entry: edit it after the run has finished.")
        try:
            database.edit_entry(entry_id, body.field, body.value, user)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @router.get("/credentials/me")
    def my_login(user: Optional[str] = Depends(current_user)):
        """Whether this user has saved an ERP login. The password is never sent back."""
        return credentials.mode_for(user)

    @router.put("/credentials/me")
    def save_my_login(body: ErpLoginIn, user: Optional[str] = Depends(current_user)):
        if not user or not settings.allowed_email_domain:
            raise HTTPException(400, "Sign-in is switched off, so the shared ERP login from .env is used.")
        try:
            enc = secret_box.encrypt(body.password)
        except secret_box.SecretBoxError as e:
            raise HTTPException(503, str(e))
        database.save_erp_credential(user, body.username.strip(), enc)
        credentials.forget_session(user)             # an old saved browser session may belong to the old login
        log.info("ERP login saved for %s", user)
        return credentials.mode_for(user)

    @router.delete("/credentials/me")
    def delete_my_login(user: Optional[str] = Depends(current_user)):
        if user:
            database.delete_erp_credential(user)
            credentials.forget_session(user)
        return credentials.mode_for(user)

    @router.post("/runs")
    def start_runs(body: RunRequest, user: Optional[str] = Depends(current_user)):
        """Queue one run per entry, one after another. Each run fills the ERP form and, only when ERP_SAVE_ENABLED=true
        and every check is clean, clicks Save."""
        mgr = get_manager()
        if credentials.mode_for(user)["mode"] == "missing":
            raise HTTPException(409, "Save your ERP login first: click 'My ERP login' at the top of the page.")
        ok, skipped = [], []
        for eid in dict.fromkeys(body.entry_ids):
            rec = database.get_realization(eid)
            if rec is None or not can_access(rec.get("owner_email"), user):
                skipped.append({"entry_id": eid, "reason": "entry not found"})
                continue
            view = build_erp_view(rec)
            if not view["can_run"]:
                skipped.append({"entry_id": eid, "reason": "; ".join(view["blockers"])})
                continue
            ok.append({"entry_id": eid, "bill_no": rec["bill_invoice_no"], "bank": rec["bank"], "requested_by": user})
        res = mgr.submit(ok) if ok else {"batch_id": None, "jobs": [], "skipped": []}
        not_queued = {s["entry_id"] for s in res["skipped"]}
        for item in ok:                      # remember when and who pressed the button, for the table
            if item["entry_id"] not in not_queued:
                database.record_erp_click(item["entry_id"], user)
        res["skipped"] = skipped + res["skipped"]
        return res

    @router.post("/entries/{entry_id}/save-resolution")
    def resolve_save(entry_id: int, body: SaveResolution, user: Optional[str] = Depends(current_user)):
        """A person checked the ERP after a Save the robot could not confirm: it WAS saved, or it was NOT."""
        rec = database.get_realization(entry_id)
        if rec is None or not can_access(rec.get("owner_email"), user):
            raise HTTPException(404, "entry not found")
        if not (rec.get("meta") or {}).get("erp_save_state"):
            raise HTTPException(409, "this entry has no unresolved save")
        if get_manager().active_for_entry(entry_id):
            raise HTTPException(409, "a run is still in progress for this entry")
        who = user or "a user"
        extra = f" ({body.note})" if body.note else ""
        if body.resolution == "saved":
            database.mark_saved_in_erp(entry_id, note=f"confirmed in the ERP by {who}{extra}", by=user)
        else:
            database.set_erp_save_state(entry_id, None, note=f"confirmed NOT saved in the ERP by {who}{extra}", by=user)
        return build_erp_view(database.get_realization(entry_id))

    @router.get("/runs/{job_id}")
    def job(job_id: str, since: int = 0, user: Optional[str] = Depends(current_user)):
        j = get_manager().get_job(job_id, since)
        if j is None or not job_ok(j, user):
            raise HTTPException(404, "run not found")
        return j

    @router.post("/runs/{job_id}/cancel")
    def cancel_job(job_id: str, user: Optional[str] = Depends(current_user)):
        j = get_manager().get_job(job_id, 10**9)
        if j is None or not job_ok(j, user):
            raise HTTPException(404, "run not found")
        return get_manager().cancel_job(job_id)

    @router.get("/entries/{entry_id}/runs")
    def entry_history(entry_id: int, limit: int = 10, user: Optional[str] = Depends(current_user)):
        rec = database.get_realization(entry_id)
        if rec is None or not can_access(rec.get("owner_email"), user):
            raise HTTPException(404, "entry not found")
        mgr = get_manager()
        with mgr.lock:
            jobs = sorted((j for j in mgr.jobs.values() if j["entry_id"] == entry_id),
                          key=lambda j: j["created_at"], reverse=True)[:limit]
            return [mgr.summary(j) for j in jobs]

    @router.get("/batches/{batch_id}")
    def batch(batch_id: str, user: Optional[str] = Depends(current_user)):
        b = get_manager().get_batch(batch_id)
        if b is None or not all(job_ok(j, user) for j in b["jobs"]):
            raise HTTPException(404, "batch not found")
        return b

    @router.post("/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str, user: Optional[str] = Depends(current_user)):
        b = get_manager().get_batch(batch_id)
        if b is None or not all(job_ok(j, user) for j in b["jobs"]):
            raise HTTPException(404, "batch not found")
        return get_manager().cancel_batch(batch_id)

    @router.get("/screens/{name}")
    def screen(name: str, user: Optional[str] = Depends(current_user)):
        if not re.fullmatch(r"[\w\-.]+\.png", name):
            raise HTTPException(400, "bad file name")
        mgr = get_manager()
        with mgr.lock:                      # a screenshot is visible only to the owner of the run that took it
            owners = [mgr.summary(j) for j in mgr.jobs.values() if any(s.get("file") == name for s in j["shots"])]
        if not owners or not all(job_ok(o, user) for o in owners):
            raise HTTPException(404, "screenshot not found")
        path = (SHOT_DIR / name).resolve()
        if SHOT_DIR.resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "screenshot not found")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    return router
