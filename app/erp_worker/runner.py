"""Background job queue for the ERP robot: one run at a time, every step tracked and saved.

The web UI never talks to Playwright directly: it submits entry ids here, then polls the job for
steps, log lines, screenshots and the final verification.  Nothing in here ever saves in the ERP.
"""
import json
import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from app.common.logger import get_logger

log = get_logger(__name__)

ACTIVE = ("queued", "running")
FINAL = ("succeeded", "attention", "failed", "cancelled")
MAX_EVENTS = 3000
KEEP_JOBS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _default_steps() -> list[dict]:
    from app.erp_worker.erp_data import STEPS      # imported lazily: needs playwright
    return [{"key": k, "label": lbl, "status": "pending", "message": "", "started_at": None, "finished_at": None}
            for k, lbl in STEPS]


def _fallback_steps() -> list[dict]:
    keys = [("validate", "Check the data before starting"), ("browser", "Start the browser and log in to the ERP"),
            ("open_form", "Open Export Proceeds Realization"), ("header", "Select the Beneficiary"),
            ("bill", "Find and pick the Bill / Invoice"), ("received", "Set the Received Date"),
            ("deductions", "Fill Deductions at Source"), ("distributions", "Fill Distributions"),
            ("verify", "Verify every filled value"), ("screenshot", "Take the final screenshot (nothing is saved)")]
    return [{"key": k, "label": lbl, "status": "pending", "message": "", "started_at": None, "finished_at": None}
            for k, lbl in keys]


class JobReporter:
    """Feeds one job from inside erp_data (same interface as erp_data.Reporter)."""

    def __init__(self, mgr: "RunManager", job: dict):
        self.mgr, self.job, self.current = mgr, job, None

    def log(self, msg: str, level: str = "info") -> None:
        self.mgr._event(self.job, level, msg, self.current)

    def step(self, key: str, status: str, msg: str = "") -> None:
        self.mgr._step(self.job, key, status, msg)
        self.current = key if status == "running" else self.current

    def shot(self, name: str, path: Path) -> None:
        self.mgr._shot(self.job, name, Path(path))

    def cancelled(self) -> bool:
        return bool(self.job.get("cancel_requested"))

    def reset_steps(self) -> None:
        self.mgr._reset_steps(self.job)


class RunManager:
    def __init__(self, run_dir: Path, loader: Callable[[int], dict], run_func: Optional[Callable] = None):
        self.run_dir = Path(run_dir)
        self.loader = loader              # entry id -> payload (read fresh from the database at run time)
        self.run_func = run_func          # run_func(payload, reporter) -> result dict; default: erp_data.run_entry
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.batches: dict[str, dict] = {}
        self.q: "queue.Queue[str]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._load_history()

    # ------------------------------------------------------------ persistence
    def _path(self, job_id: str) -> Path:
        return self.run_dir / f"job_{job_id}.json"

    def _save(self, job: dict) -> None:
        try:
            tmp = self._path(job["id"]).with_suffix(".tmp")
            tmp.write_text(json.dumps(job, default=str), encoding="utf-8")
            tmp.replace(self._path(job["id"]))
        except OSError as e:
            log.warning("could not save job %s: %s", job["id"], e)

    def _load_history(self) -> None:
        files = sorted(self.run_dir.glob("job_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:KEEP_JOBS]
        for f in files:
            try:
                job = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if job.get("state") in ACTIVE:           # the app restarted while this one was running
                job["state"] = "failed"
                job["finished_at"] = _now()
                job["error"] = {"code": "INTERRUPTED", "title": "Run interrupted",
                                "message": "The application restarted while this run was in progress.",
                                "hint": "Nothing was saved in the ERP. Run the entry again.", "retryable": True,
                                "details": {}}
                for st in job.get("steps", []):
                    if st["status"] in ("running", "pending"):
                        st["status"] = "skipped"
                self._save(job)
            self.jobs[job["id"]] = job
            b = job.get("batch_id")
            if b:
                self.batches.setdefault(b, {"id": b, "created_at": job["created_at"], "job_ids": []})["job_ids"].append(job["id"])

    # ------------------------------------------------------------ job internals
    def _event(self, job: dict, level: str, msg: str, step: Optional[str]) -> None:
        with self.lock:
            ev = job["events"]
            if len(ev) >= MAX_EVENTS:
                return
            ev.append({"i": job["event_count"], "t": _now(), "level": level, "msg": msg, "step": step})
            job["event_count"] += 1

    def _step(self, job: dict, key: str, status: str, msg: str) -> None:
        with self.lock:
            st = next((s for s in job["steps"] if s["key"] == key), None)
            if st is None:
                return
            st["status"] = status
            st["message"] = msg or st["message"]
            if status == "running":
                st["started_at"], st["finished_at"] = _now(), None
                job["current_step"] = key
            elif status in ("done", "failed", "skipped", "warn"):
                st["finished_at"] = _now()
            mark = {"running": "▶", "done": "✓", "failed": "✗", "skipped": "–", "warn": "!"}.get(status, "•")
            level = {"failed": "error", "warn": "warn", "done": "ok"}.get(status, "step")
            text = f"{mark} {st['label']}" + (f" — {msg}" if msg else "")
            self._event(job, level, text, key)
        if status in ("done", "failed", "skipped"):
            self._save(job)

    def _shot(self, job: dict, name: str, path: Path) -> None:
        with self.lock:
            job["shots"].append({"file": path.name, "label": name, "t": _now(), "step": job.get("current_step")})
            self._event(job, "info", f"Screenshot: {name}", job.get("current_step"))

    def _reset_steps(self, job: dict) -> None:
        with self.lock:
            for st in job["steps"]:
                if st["key"] != "validate":
                    st.update(status="pending", message="", started_at=None, finished_at=None)
            self._event(job, "warn", "Starting again from the first step", None)

    # ------------------------------------------------------------ public API
    def active_for_entry(self, entry_id: int) -> Optional[dict]:
        with self.lock:
            return next((j for j in self.jobs.values() if j["entry_id"] == entry_id and j["state"] in ACTIVE), None)

    def submit(self, entries: list[dict]) -> dict:
        """entries: [{'entry_id', 'bill_no', 'bank'}].  Returns {'batch_id', 'jobs': [...], 'skipped': [...]}."""
        batch_id = uuid.uuid4().hex[:10]
        out_jobs, skipped = [], []
        with self.lock:
            for e in entries:
                dup = self.active_for_entry(e["entry_id"])
                if dup:
                    skipped.append({"entry_id": e["entry_id"], "reason": f"already {dup['state']} (job {dup['id']})",
                                    "job_id": dup["id"]})
                    continue
                job = {
                    "id": uuid.uuid4().hex[:10], "batch_id": batch_id, "entry_id": e["entry_id"],
                    "bill_no": e.get("bill_no"), "bank": e.get("bank"), "state": "queued",
                    "created_at": _now(), "started_at": None, "finished_at": None, "current_step": None,
                    "steps": _fallback_steps(), "events": [], "event_count": 0, "shots": [],
                    "result": None, "error": None, "attempts": 0, "cancel_requested": False,
                }
                self._event(job, "info", "Waiting in the queue", None)
                self.jobs[job["id"]] = job
                self._save(job)
                out_jobs.append(job["id"])
                self.q.put(job["id"])
            if out_jobs:
                self.batches[batch_id] = {"id": batch_id, "created_at": _now(), "job_ids": out_jobs}
        self._ensure_worker()
        return {"batch_id": batch_id if out_jobs else None, "jobs": out_jobs, "skipped": skipped}

    def get_job(self, job_id: str, since: int = 0) -> Optional[dict]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            out = {k: v for k, v in job.items() if k not in ("events",)}
            out["steps"] = [dict(s) for s in job["steps"]]
            out["shots"] = list(job["shots"])
            out["events"] = [e for e in job["events"] if e["i"] >= since]
            out["next_since"] = job["event_count"]
            return json.loads(json.dumps(out, default=str))

    @staticmethod
    def _remark(job: dict) -> Optional[dict]:
        """Very short reason shown in the table's Remarks column (only for problems / uncertainty)."""
        st = job.get("state")
        if st == "failed":
            e = job.get("error") or {}
            text = e.get("title") or "Failed"
            if e.get("code") == "UNEXPECTED" and e.get("message"):
                text = f"{text}: {e['message']}"
            return {"level": "error", "text": text[:90]}
        if st == "attention":
            checks = (((job.get("result") or {}).get("verification") or {}).get("checks")) or []
            bad = [c for c in checks if c.get("status") in ("error", "warn")]
            pre = {"Deductions at Source": "Ded ", "Distributions": "Dist "}
            if not bad:
                return {"level": "warn", "text": "Some values need checking"}
            c = bad[0]
            first = f"{pre.get(c['area'], '')}{c['field']}: {c.get('expected') or '—'} ≠ {c.get('erp') or '—'}"
            more = f" (+{len(bad) - 1} more)" if len(bad) > 1 else ""
            return {"level": "warn", "text": (first[:70] + more)}
        if st == "cancelled":
            return {"level": "info", "text": "Cancelled"}
        return None

    @staticmethod
    def summary(job: dict) -> dict:
        res = job.get("result") or {}
        counts = ((res.get("verification") or {}).get("counts")) or None
        step = next((s for s in job["steps"] if s["key"] == job.get("current_step")), None)
        return {"id": job["id"], "batch_id": job.get("batch_id"), "entry_id": job["entry_id"], "state": job["state"],
                "created_at": job["created_at"], "started_at": job.get("started_at"), "finished_at": job.get("finished_at"),
                "current_step": job.get("current_step"), "current_label": step["label"] if step else None,
                "error": ({"code": job["error"].get("code"), "title": job["error"].get("title")} if job.get("error") else None),
                "counts": counts, "attempts": job.get("attempts", 0), "remark": RunManager._remark(job)}

    def latest_by_entry(self) -> dict[int, dict]:
        with self.lock:
            latest: dict[int, dict] = {}
            for j in self.jobs.values():
                cur = latest.get(j["entry_id"])
                if cur is None or j["created_at"] >= cur["created_at"]:
                    latest[j["entry_id"]] = j
            return {k: self.summary(v) for k, v in latest.items()}

    def get_batch(self, batch_id: str) -> Optional[dict]:
        with self.lock:
            b = self.batches.get(batch_id)
            if b is None:
                return None
            jobs = [self.summary(self.jobs[i]) for i in b["job_ids"] if i in self.jobs]
            states = [j["state"] for j in jobs]
            done = sum(1 for s in states if s in FINAL)
            return {"id": batch_id, "created_at": b["created_at"], "jobs": jobs, "total": len(jobs), "done": done,
                    "finished": done == len(jobs),
                    "counts": {k: states.count(k) for k in ("queued", "running", "succeeded", "attention", "failed", "cancelled")}}

    def cancel_job(self, job_id: str) -> Optional[dict]:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job["state"] in ACTIVE:
                job["cancel_requested"] = True
                if job["state"] == "queued":
                    self._finish(job, "cancelled", None, None)
                else:
                    self._event(job, "warn", "Cancel requested: stopping at the next safe point", job.get("current_step"))
            return self.summary(job)

    def cancel_batch(self, batch_id: str) -> Optional[dict]:
        with self.lock:
            b = self.batches.get(batch_id)
            if b is None:
                return None
            for jid in b["job_ids"]:
                self.cancel_job(jid)
        return self.get_batch(batch_id)

    def worker_status(self) -> dict:
        with self.lock:
            running = next((j for j in self.jobs.values() if j["state"] == "running"), None)
            return {"busy": running is not None, "running_job": running["id"] if running else None,
                    "queued": sum(1 for j in self.jobs.values() if j["state"] == "queued")}

    # ------------------------------------------------------------ worker
    def _ensure_worker(self) -> None:
        with self.lock:
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._loop, name="erp-worker", daemon=True)
                self.worker.start()

    def _finish(self, job: dict, state: str, result: Optional[dict], error: Optional[dict]) -> None:
        with self.lock:
            job["state"] = state
            job["finished_at"] = _now()
            job["result"], job["error"] = result, error
            for st in job["steps"]:
                if st["status"] in ("pending", "running"):
                    st["status"] = "skipped"
            label = {"succeeded": "Finished: the ERP form is filled. NOTHING WAS SAVED.",
                     "attention": "Finished: the form is filled but some values need your attention. NOTHING WAS SAVED.",
                     "failed": "Stopped because of an error. NOTHING WAS SAVED.",
                     "cancelled": "Cancelled. NOTHING WAS SAVED."}[state]
            self._event(job, {"succeeded": "ok", "attention": "warn", "failed": "error", "cancelled": "warn"}[state], label, None)
        self._save(job)

    def _loop(self) -> None:
        while True:
            job_id = self.q.get()
            job = self.jobs.get(job_id)
            if job is None or job["state"] != "queued":
                continue
            try:
                self._run(job)
            except Exception as e:      # the worker must never die
                log.exception("erp worker crashed on job %s", job_id)
                self._finish(job, "failed", None, {"code": "UNEXPECTED", "title": "Unexpected problem",
                                                   "message": str(e)[:300], "hint": "Tell the developer.",
                                                   "retryable": False, "details": {}})

    def _run(self, job: dict) -> None:
        with self.lock:
            if job["cancel_requested"]:
                self._finish(job, "cancelled", None, None)
                return
            job["state"], job["started_at"] = "running", _now()
            self._event(job, "info", f"Started entry {job['entry_id']} (bill {job.get('bill_no')})", None)
        reporter = JobReporter(self, job)
        try:
            payload = self.loader(job["entry_id"])
        except Exception as e:
            self._finish(job, "failed", None, {"code": "VALIDATION", "title": "The entry is not ready",
                                               "message": f"Could not read the entry from the database: {e}",
                                               "hint": "Check that the entry still exists.", "retryable": False, "details": {}})
            return
        try:
            run = self.run_func
            if run is None:
                from app.erp_worker import erp_data
                with self.lock:
                    job["steps"] = _default_steps()
                run = erp_data.run_entry
        except ImportError as e:
            self._finish(job, "failed", None, {"code": "CONFIG", "title": "Robot is not installed",
                                               "message": f"Playwright is not available in this container: {e}",
                                               "hint": "Rebuild the app image: docker compose build app",
                                               "retryable": False, "details": {}})
            return
        result = run(payload, reporter)
        with self.lock:
            job["attempts"] = result.get("attempts", 0)
        outcome = result.get("outcome", "failed")
        state = outcome if outcome in FINAL else "failed"
        self._finish(job, state, {k: v for k, v in result.items() if k != "error"}, result.get("error"))
