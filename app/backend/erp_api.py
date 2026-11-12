"""ERP endpoints for the web UI: the table (what will be filled), and the tracked robot runs."""
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.common.logger import get_logger
from app.components import database
from app.config.settings import settings
from app.erp_worker.runner import RunManager
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
        _manager = RunManager(RUN_DIR, _load_payload)
    return _manager


class RunRequest(BaseModel):
    entry_ids: list[int] = Field(min_length=1, max_length=100)


def build_router(require_key, current_user) -> APIRouter:
    router = APIRouter(prefix="/erp", dependencies=[Depends(require_key), Depends(current_user)])

    @router.get("/entries")
    def entries(status: Optional[str] = None, needs_review: Optional[bool] = None):
        mgr = get_manager()
        latest = mgr.latest_by_entry()
        rows = sorted(database.list_realizations(status, needs_review), key=lambda r: r["id"], reverse=True)
        out = []
        for rec in rows:
            v = build_erp_view(rec)
            v["last_run"] = latest.get(rec["id"])
            out.append(v)
        return {"entries": out, "worker": mgr.worker_status(),
                "erp_configured": bool(os.getenv("ERP_USERNAME") and os.getenv("ERP_PASSWORD"))}

    @router.post("/runs")
    def start_runs(body: RunRequest, user: Optional[str] = Depends(current_user)):
        """Queue one run per entry. Runs happen one after another; nothing is ever saved in the ERP."""
        mgr = get_manager()
        ok, skipped = [], []
        for eid in dict.fromkeys(body.entry_ids):
            rec = database.get_realization(eid)
            if rec is None:
                skipped.append({"entry_id": eid, "reason": "entry not found"})
                continue
            view = build_erp_view(rec)
            if not view["can_run"]:
                skipped.append({"entry_id": eid, "reason": "; ".join(view["blockers"])})
                continue
            ok.append({"entry_id": eid, "bill_no": rec["bill_invoice_no"], "bank": rec["bank"]})
        res = mgr.submit(ok) if ok else {"batch_id": None, "jobs": [], "skipped": []}
        not_queued = {s["entry_id"] for s in res["skipped"]}
        for item in ok:                      # remember when and who pressed the button, for the table
            if item["entry_id"] not in not_queued:
                database.record_erp_click(item["entry_id"], user)
        res["skipped"] = skipped + res["skipped"]
        return res

    @router.get("/runs/{job_id}")
    def job(job_id: str, since: int = 0):
        j = get_manager().get_job(job_id, since)
        if j is None:
            raise HTTPException(404, "run not found")
        return j

    @router.post("/runs/{job_id}/cancel")
    def cancel_job(job_id: str):
        s = get_manager().cancel_job(job_id)
        if s is None:
            raise HTTPException(404, "run not found")
        return s

    @router.get("/entries/{entry_id}/runs")
    def entry_history(entry_id: int, limit: int = 10):
        mgr = get_manager()
        with mgr.lock:
            jobs = sorted((j for j in mgr.jobs.values() if j["entry_id"] == entry_id),
                          key=lambda j: j["created_at"], reverse=True)[:limit]
            return [mgr.summary(j) for j in jobs]

    @router.get("/batches/{batch_id}")
    def batch(batch_id: str):
        b = get_manager().get_batch(batch_id)
        if b is None:
            raise HTTPException(404, "batch not found")
        return b

    @router.post("/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str):
        b = get_manager().cancel_batch(batch_id)
        if b is None:
            raise HTTPException(404, "batch not found")
        return b

    @router.get("/screens/{name}")
    def screen(name: str):
        if not re.fullmatch(r"[\w\-.]+\.png", name):
            raise HTTPException(400, "bad file name")
        path = (SHOT_DIR / name).resolve()
        if SHOT_DIR.resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "screenshot not found")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})

    return router
